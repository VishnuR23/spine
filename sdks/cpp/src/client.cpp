#include "spine/client.hpp"

#include <curl/curl.h>

#include <array>
#include <atomic>
#include <mutex>
#include <string>
#include <vector>

namespace spine {

const char* to_string(Decision d) {
    switch (d) {
        case Decision::Allowed: return "allowed";
        case Decision::Flagged: return "flagged";
        case Decision::Blocked: break;
    }
    return "blocked";
}

namespace detail {

std::string json_escape(const std::string& raw) {
    std::string out;
    out.reserve(raw.size() + 8);
    for (unsigned char c : raw) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            default:
                if (c < 0x20) {
                    // Control characters must be escaped or the server rejects
                    // the body. Symbols and venue codes should never contain
                    // these, but metadata sometimes carries free text.
                    static const char* kHex = "0123456789abcdef";
                    out += "\\u00";
                    out += kHex[(c >> 4) & 0xF];
                    out += kHex[c & 0xF];
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

std::string build_intercept_body(const Action& action) {
    std::string body;
    body.reserve(256);
    body += "{\"agent_id\":\"";
    body += json_escape(action.agent_id);
    body += "\",\"action\":{\"action_type\":\"";
    body += json_escape(action.action_type);
    body += "\",\"target_resource\":\"";
    body += json_escape(action.target_resource);
    body += "\"";

    if (!action.metadata.empty()) {
        body += ",\"metadata\":{";
        bool first = true;
        for (const auto& kv : action.metadata) {
            if (!first) body += ",";
            first = false;
            body += "\"";
            body += json_escape(kv.first);
            body += "\":\"";
            body += json_escape(kv.second);
            body += "\"";
        }
        body += "}";
    }
    body += "}";

    if (!action.session_id.empty()) {
        body += ",\"session_id\":\"";
        body += json_escape(action.session_id);
        body += "\"";
    }
    body += "}";
    return body;
}

// Minimal field extraction. We control both ends of this contract and the
// response is a flat object of scalars, so a full parser would be a
// dependency bought for nothing. If the response shape ever nests, replace
// this rather than extending it.
static bool find_string_field(const std::string& body, const std::string& key,
                              std::string* out) {
    const std::string needle = "\"" + key + "\"";
    size_t pos = body.find(needle);
    if (pos == std::string::npos) return false;
    pos = body.find(':', pos + needle.size());
    if (pos == std::string::npos) return false;
    ++pos;
    while (pos < body.size() && (body[pos] == ' ' || body[pos] == '\t')) ++pos;
    if (pos >= body.size() || body[pos] != '"') return false;  // null, or not a string
    ++pos;

    std::string value;
    while (pos < body.size() && body[pos] != '"') {
        if (body[pos] == '\\' && pos + 1 < body.size()) {
            ++pos;
            switch (body[pos]) {
                case 'n': value += '\n'; break;
                case 'r': value += '\r'; break;
                case 't': value += '\t'; break;
                case 'b': value += '\b'; break;
                case 'f': value += '\f'; break;
                case 'u':
                    // Skipped rather than decoded; these fields are
                    // human-readable reasons, not values we act on.
                    if (pos + 4 < body.size()) pos += 4;
                    break;
                default: value += body[pos];
            }
        } else {
            value += body[pos];
        }
        ++pos;
    }
    *out = value;
    return true;
}

static bool find_bool_field(const std::string& body, const std::string& key,
                            bool* out) {
    const std::string needle = "\"" + key + "\"";
    size_t pos = body.find(needle);
    if (pos == std::string::npos) return false;
    pos = body.find(':', pos + needle.size());
    if (pos == std::string::npos) return false;
    ++pos;
    while (pos < body.size() && (body[pos] == ' ' || body[pos] == '\t')) ++pos;
    if (body.compare(pos, 4, "true") == 0)  { *out = true;  return true; }
    if (body.compare(pos, 5, "false") == 0) { *out = false; return true; }
    return false;
}

Result parse_intercept_response(const std::string& body) {
    Result r;
    bool allowed = false;
    if (!find_bool_field(body, "allowed", &allowed)) {
        // A response we cannot read is not permission.
        r.allowed = false;
        r.decision = Decision::Blocked;
        r.reason = "unparseable response from Spine";
        r.failed_closed = true;
        return r;
    }
    r.allowed = allowed;

    std::string decision;
    find_string_field(body, "decision", &decision);
    if (decision == "allowed") {
        r.decision = Decision::Allowed;
    } else if (decision == "flagged") {
        r.decision = Decision::Flagged;
    } else {
        r.decision = Decision::Blocked;
    }

    find_string_field(body, "reason", &r.reason);
    find_string_field(body, "audit_event_id", &r.audit_event_id);
    return r;
}

}  // namespace detail

namespace {

size_t write_cb(char* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* buf = static_cast<std::string*>(userdata);
    buf->append(ptr, size * nmemb);
    return size * nmemb;
}

// curl_global_init is not thread-safe and must run once per process. A
// function-local static gives that guarantee without asking callers to
// remember an init call.
void ensure_curl_initialised() {
    struct GlobalInit {
        GlobalInit() { curl_global_init(CURL_GLOBAL_DEFAULT); }
        ~GlobalInit() { curl_global_cleanup(); }
    };
    static GlobalInit once;
    (void)once;
}

}  // namespace

namespace detail {

struct Shared {
    explicit Shared(bool reuse_connections) : reuse(reuse_connections) {
        for (auto& b : buckets) b.store(0);
    }
    ~Shared() {
        for (CURL* h : pool) curl_easy_cleanup(h);
    }
    Shared(const Shared&) = delete;
    Shared& operator=(const Shared&) = delete;

    CURL* acquire() {
        if (reuse) {
            std::lock_guard<std::mutex> g(pool_mu);
            if (!pool.empty()) {
                CURL* h = pool.back();
                pool.pop_back();
                return h;
            }
        }
        return curl_easy_init();
    }

    // A handle that just timed out or hit a transport error goes away rather
    // than back in the pool: its connection state is not worth trusting.
    void release(CURL* h, bool healthy) {
        if (h == nullptr) return;
        if (!reuse || !healthy) {
            curl_easy_cleanup(h);
            return;
        }
        curl_easy_reset(h);  // clears options, keeps the live connection
        std::lock_guard<std::mutex> g(pool_mu);
        pool.push_back(h);
    }

    void count(const Result& r) {
        switch (r.decision) {
            case Decision::Allowed: ++allowed; break;
            case Decision::Flagged: ++flagged; break;
            case Decision::Blocked: ++blocked; break;
        }
    }

    void observe(std::chrono::microseconds latency) {
        const long long us = latency.count();
        size_t i = 0;
        while (i < Stats::kBucketUpperMs.size() && us > Stats::kBucketUpperMs[i] * 1000) ++i;
        ++buckets[i];
        long long prev = max_latency_us.load();
        while (us > prev && !max_latency_us.compare_exchange_weak(prev, us)) {
        }
    }

    const bool reuse;
    std::mutex pool_mu;
    std::vector<CURL*> pool;

    std::atomic<std::uint64_t> allowed{0};
    std::atomic<std::uint64_t> blocked{0};
    std::atomic<std::uint64_t> flagged{0};
    std::atomic<std::uint64_t> failed_closed_n{0};
    std::atomic<std::uint64_t> short_circuited_n{0};
    std::array<std::atomic<std::uint64_t>, 8> buckets;
    std::atomic<long long> max_latency_us{0};
};

}  // namespace detail

Client::Client(Config config) : config_(std::move(config)) {
    ensure_curl_initialised();
    if (!config_.base_url.empty() && config_.base_url.back() == '/') {
        config_.base_url.pop_back();
    }
    shared_ = std::make_shared<detail::Shared>(config_.reuse_connections);
}

Result Client::intercept(const Action& action) const {
    return intercept_with_budget(action, config_.timeout);
}

Result Client::intercept_with_budget(const Action& action,
                                     std::chrono::milliseconds budget) const {
    Result r = perform(action, budget);
    if (r.failed_closed) ++shared_->failed_closed_n;
    shared_->observe(r.latency);
    shared_->count(r);
    return r;
}

Stats Client::stats() const {
    Stats s;
    s.allowed = shared_->allowed.load();
    s.blocked = shared_->blocked.load();
    s.flagged = shared_->flagged.load();
    s.failed_closed = shared_->failed_closed_n.load();
    s.short_circuited = shared_->short_circuited_n.load();
    for (size_t i = 0; i < s.latency_buckets.size(); ++i) s.latency_buckets[i] = shared_->buckets[i].load();
    s.max_latency = std::chrono::microseconds(shared_->max_latency_us.load());
    return s;
}

Result Client::perform(const Action& action, std::chrono::milliseconds budget) const {
    const auto started = std::chrono::steady_clock::now();

    Result fallback;
    fallback.allowed = config_.fail_open;
    fallback.decision = config_.fail_open ? Decision::Allowed : Decision::Blocked;
    fallback.failed_closed = true;

    CURL* curl = shared_->acquire();
    if (curl == nullptr) {
        fallback.reason = "could not initialise HTTP client";
        fallback.latency = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - started);
        return fallback;
    }

    const std::string url = config_.base_url + "/v1/intercept";
    const std::string body = detail::build_intercept_body(action);
    std::string response;

    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    // Without this libcurl may wait for a "100 Continue" before sending.
    headers = curl_slist_append(headers, "Expect:");
    const std::string key_header = "X-Org-Key: " + config_.org_key;
    headers = curl_slist_append(headers, key_header.c_str());

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    // The whole call is bounded by the budget, not just the connect phase.
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, static_cast<long>(budget.count()));
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);

    const CURLcode rc = curl_easy_perform(curl);
    long status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);

    curl_slist_free_all(headers);
    shared_->release(curl, rc == CURLE_OK);

    const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - started);

    if (rc != CURLE_OK) {
        fallback.reason = (rc == CURLE_OPERATION_TIMEDOUT)
            ? "latency budget exceeded"
            : std::string("transport error: ") + curl_easy_strerror(rc);
        fallback.latency = elapsed;
        return fallback;
    }

    if (status != 200) {
        // 401, 403, 422 and friends are all "no", not "maybe".
        fallback.reason = "Spine returned HTTP " + std::to_string(status);
        fallback.latency = elapsed;
        return fallback;
    }

    Result r = detail::parse_intercept_response(response);
    r.latency = elapsed;
    return r;
}

}  // namespace spine
