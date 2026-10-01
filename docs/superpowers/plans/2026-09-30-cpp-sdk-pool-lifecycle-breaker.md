# C++ SDK: Pool, Lifecycle, Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `sdks/cpp` connection reuse, amend/cancel/market-order coverage, in-process stats, a circuit breaker, and a manual halt.

**Architecture:** `Client` gains a `std::shared_ptr<detail::Shared>` holding a mutex-guarded pool of libcurl easy handles, atomic counters, breaker state, and halt state; copies of a `Client` share it. Every intercept goes through one private `intercept_impl`, which applies halt → breaker admission → network call → breaker settlement → stats, and a `risk_reducing` flag that turns local verdicts into "allow" for cancels. The finance layer adds pricing rules and two new `OrderGate` checks on top.

**Tech Stack:** C++17, libcurl, CMake ≥ 3.16, POSIX sockets and threads (tests only for sockets).

**Spec:** `docs/superpowers/specs/2026-09-30-cpp-sdk-pool-lifecycle-breaker-design.md`

## Global Constraints

- Library depends only on libcurl and the C++17 standard library (`Threads::Threads` is the standard library's threading, allowed).
- Nothing on the order path throws. User callbacks are called inside `try { } catch (...) { }`.
- Spine decides; the client only chooses which question to ask.
- Existing public API keeps compiling. Existing aggregate initialisation `Order{"AAPL", Side::Buy, 100, 150.0, "XNAS", "stat-arb", "t-1"}` must still compile, so new `Order` fields go after `trader_id`.
- No server changes.
- Defaults: `reuse_connections = true`, `breaker_threshold = 5`, `breaker_cooldown = 5 s`, `market_collar = 1.05`.
- Latency bucket upper bounds (inclusive, ms): 1, 2, 5, 10, 20, 50, 100, plus overflow.
- Cancel local-verdict reason prefix: `cancel allowed locally: `.
- Compile flags `-Wall -Wextra -Wpedantic` stay clean.
- Build/test commands (from `sdks/cpp`): `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j4 && ./build/spine_tests`.

## Review Focus

1. **A pooled connection the server has closed** (Spine restarted, or `Connection: close`) — the next call must still get a real verdict. Pinned in Task 3 (`test_server_closing_connections_is_harmless`).
2. **A call that times out on a pooled handle** — the handle must be dropped and the next call must work. Pinned in Task 3 (`test_timeout_then_recovery`).
3. **Many threads sharing one Client** — no crash, and stats add up exactly. Pinned in Task 4 (`test_stats_are_exact_under_concurrency`).
4. **`fail_open = true` while the breaker is open** — short-circuit must honour `fail_open`, but a halt must still block. Pinned in Task 5 and Task 6.
5. **An `on_breaker_change` callback that throws** — the intercept must not throw. Pinned in Task 5 (`test_throwing_callback_is_contained`).

---

## File Structure

- `sdks/cpp/include/spine/client.hpp` — modify: `Config` fields, `Stats`, `BreakerState`, new `Client` methods, private `intercept_impl`/`perform`, `shared_` member.
- `sdks/cpp/src/client.cpp` — modify: `detail::Shared` (pool, counters, breaker, halt), new methods.
- `sdks/cpp/include/spine/finance.hpp` — modify: `Order` fields, `NotionalBands::market_collar`, `Pricing`, overloads, `check_amend`, `check_cancel`, `client()`.
- `sdks/cpp/src/finance.cpp` — modify: pricing, metadata helper, new checks.
- `sdks/cpp/tests/check.hpp` — create: shared `check`/`check_eq` helpers and suite declarations.
- `sdks/cpp/tests/fake_spine.hpp` — create: test-only HTTP server.
- `sdks/cpp/tests/test_client.cpp` — modify: use `check.hpp`, `main` runs every suite.
- `sdks/cpp/tests/test_transport.cpp` — create: pool, stats, breaker, halt tests.
- `sdks/cpp/tests/test_lifecycle.cpp` — create: pricing, amend, cancel tests.
- `sdks/cpp/CMakeLists.txt` — modify: multi-file test target, Threads.
- `sdks/cpp/examples/pretrade_gate.cpp` — modify: show amend, cancel, stats.
- `examples/finance/seed_finance_policies.py` — modify: amend and cancel policies.
- `sdks/cpp/README.md`, `CHANGELOG.md` — modify: document it.

---

### Task 1: Split the test harness

No behavior change. Lets later tasks add test files.

**Files:**
- Create: `sdks/cpp/tests/check.hpp`
- Modify: `sdks/cpp/tests/test_client.cpp`, `sdks/cpp/CMakeLists.txt`

**Interfaces:**
- Produces: `spine_test::check(bool, const char*)`, `spine_test::check_eq(const std::string&, const std::string&, const char*)`, `spine_test::g_checks`, `spine_test::g_failures`, and suite entry points `void run_client_tests(); void run_transport_tests(); void run_lifecycle_tests();`.

- [ ] **Step 1: Create `tests/check.hpp`**

```cpp
// Shared assertion helpers for the C++ SDK tests. No framework, on purpose:
// see the comment at the top of test_client.cpp.

#ifndef SPINE_TESTS_CHECK_HPP
#define SPINE_TESTS_CHECK_HPP

#include <cstdio>
#include <string>

namespace spine_test {

inline int g_failures = 0;
inline int g_checks = 0;

inline void check(bool condition, const char* what) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::printf("  FAIL  %s\n", what);
    }
}

inline void check_eq(const std::string& actual, const std::string& expected,
                     const char* what) {
    ++g_checks;
    if (actual != expected) {
        ++g_failures;
        std::printf("  FAIL  %s\n        expected: %s\n        actual:   %s\n",
                    what, expected.c_str(), actual.c_str());
    }
}

}  // namespace spine_test

void run_client_tests();
void run_transport_tests();
void run_lifecycle_tests();

#endif  // SPINE_TESTS_CHECK_HPP
```

- [ ] **Step 2: Point `test_client.cpp` at it**

In `tests/test_client.cpp`: delete the local `g_failures`, `g_checks`, `check`, and `check_eq` definitions inside the anonymous namespace; add `#include "check.hpp"` after the spine includes and `using spine_test::check; using spine_test::check_eq;` at the top of the anonymous namespace. Replace `main` with:

```cpp
void run_client_tests() {
    test_json_escaping();
    test_body_construction();
    test_response_parsing();
    test_notional_and_banding();
    test_fails_closed_when_spine_is_unreachable();
    test_fail_open_is_available_but_explicit();
}

int main() {
    std::printf("\nspine C++ client tests\n\n");
    run_client_tests();
    run_transport_tests();
    run_lifecycle_tests();
    std::printf("\n  %d checks, %d failures\n\n", spine_test::g_checks, spine_test::g_failures);
    return spine_test::g_failures == 0 ? 0 : 1;
}
```

Create `tests/test_transport.cpp` and `tests/test_lifecycle.cpp`, each containing only:

```cpp
#include "check.hpp"

void run_transport_tests() {}
```

(and `run_lifecycle_tests` respectively).

- [ ] **Step 3: Build the test binary from all three files**

In `CMakeLists.txt` replace the test block with:

```cmake
if(SPINE_BUILD_TESTS)
    enable_testing()
    add_executable(spine_tests
        tests/test_client.cpp
        tests/test_transport.cpp
        tests/test_lifecycle.cpp
    )
    target_link_libraries(spine_tests PRIVATE spine_client Threads::Threads)
    add_test(NAME spine_tests COMMAND spine_tests)
endif()
```

and after `find_package(CURL REQUIRED)` add `find_package(Threads REQUIRED)`, and change the library link line to `target_link_libraries(spine_client PUBLIC CURL::libcurl Threads::Threads)`.

- [ ] **Step 4: Build and run**

Run: `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j4 && ./build/spine_tests`
Expected: `39 checks, 0 failures`, no compiler warnings.

- [ ] **Step 5: Commit** — `test(cpp): split the test binary into suites with a shared check header`

---

### Task 2: Fake Spine test server

**Files:**
- Create: `sdks/cpp/tests/fake_spine.hpp`
- Modify: `sdks/cpp/tests/test_transport.cpp`

**Interfaces:**
- Produces: `spine_test::FakeSpine` with `url()`, `respond(int status, std::string body)`, `set_delay(std::chrono::milliseconds)`, `set_close_each(bool)`, `connections()`, `requests()`, `last_body()`, `stop()`. Also `spine_test::kAllowed`, `kBlocked`, `kFlagged` canned bodies.

- [ ] **Step 1: Write the server**

```cpp
// A test-only stand-in for Spine's intercept endpoint.
//
// Real HTTP/1.1 over a loopback socket, so the client's actual libcurl path
// is exercised, including keep-alive. Counts accepted connections and
// requests, which is how the tests prove connection reuse and that a tripped
// breaker makes no network call. POSIX only; never linked into the library.

#ifndef SPINE_TESTS_FAKE_SPINE_HPP
#define SPINE_TESTS_FAKE_SPINE_HPP

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace spine_test {

inline const char* kAllowed =
    R"({"allowed":true,"decision":"allowed","reason":"ok","audit_event_id":"ev-1"})";
inline const char* kBlocked =
    R"({"allowed":false,"decision":"blocked","reason":"Restricted list"})";
inline const char* kFlagged =
    R"({"allowed":false,"decision":"flagged","reason":"Four-eyes review"})";

class FakeSpine {
public:
    FakeSpine() {
        listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        int one = 1;
        ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port = 0;  // any free port
        ::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof addr);
        ::listen(listen_fd_, 64);
        socklen_t len = sizeof addr;
        ::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&addr), &len);
        port_ = ntohs(addr.sin_port);
        acceptor_ = std::thread([this] { accept_loop(); });
    }

    ~FakeSpine() { stop(); }
    FakeSpine(const FakeSpine&) = delete;
    FakeSpine& operator=(const FakeSpine&) = delete;

    std::string url() const { return "http://127.0.0.1:" + std::to_string(port_); }

    void respond(int status, std::string body) {
        std::lock_guard<std::mutex> g(mu_);
        status_ = status;
        body_ = std::move(body);
    }
    void set_delay(std::chrono::milliseconds d) { delay_ms_ = static_cast<int>(d.count()); }
    void set_close_each(bool v) { close_each_ = v; }

    int connections() const { return connections_.load(); }
    int requests() const { return requests_.load(); }
    std::string last_body() const {
        std::lock_guard<std::mutex> g(mu_);
        return last_body_;
    }

    void stop() {
        if (stopped_.exchange(true)) return;
        if (acceptor_.joinable()) acceptor_.join();
        ::close(listen_fd_);
        std::vector<std::thread> workers;
        {
            std::lock_guard<std::mutex> g(mu_);
            workers.swap(workers_);
        }
        for (auto& t : workers) t.join();
    }

private:
    // Poll with a short timeout rather than block, so stop() works without
    // closing sockets out from under another thread (portable to macOS).
    bool readable(int fd) {
        while (!stopped_) {
            pollfd p{fd, POLLIN, 0};
            const int n = ::poll(&p, 1, 20);
            if (n > 0) return true;
            if (n < 0) return false;
        }
        return false;
    }

    void accept_loop() {
        while (readable(listen_fd_)) {
            const int fd = ::accept(listen_fd_, nullptr, nullptr);
            if (fd < 0) continue;
            ++connections_;
            std::lock_guard<std::mutex> g(mu_);
            workers_.emplace_back([this, fd] { serve(fd); });
        }
    }

    bool read_more(int fd, std::string* buf) {
        if (!readable(fd)) return false;
        char chunk[4096];
        const ssize_t n = ::recv(fd, chunk, sizeof chunk, 0);
        if (n <= 0) return false;
        buf->append(chunk, static_cast<size_t>(n));
        return true;
    }

    static size_t content_length(const std::string& headers) {
        std::string lower(headers);
        std::transform(lower.begin(), lower.end(), lower.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        const size_t pos = lower.find("content-length:");
        if (pos == std::string::npos) return 0;
        return static_cast<size_t>(std::strtoul(lower.c_str() + pos + 15, nullptr, 10));
    }

    void send_all(int fd, const std::string& data) {
#ifdef MSG_NOSIGNAL
        const int flags = MSG_NOSIGNAL;
#else
        const int flags = 0;
        int one = 1;
        ::setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
        size_t sent = 0;
        while (sent < data.size()) {
            const ssize_t n = ::send(fd, data.data() + sent, data.size() - sent, flags);
            if (n <= 0) return;
            sent += static_cast<size_t>(n);
        }
    }

    void serve(int fd) {
        std::string buf;
        for (;;) {
            size_t header_end;
            while ((header_end = buf.find("\r\n\r\n")) == std::string::npos) {
                if (!read_more(fd, &buf)) { ::close(fd); return; }
            }
            const size_t len = content_length(buf.substr(0, header_end));
            const size_t total = header_end + 4 + len;
            while (buf.size() < total) {
                if (!read_more(fd, &buf)) { ::close(fd); return; }
            }
            int status;
            std::string body;
            {
                std::lock_guard<std::mutex> g(mu_);
                last_body_ = buf.substr(header_end + 4, len);
                status = status_;
                body = body_;
            }
            buf.erase(0, total);
            ++requests_;

            const int delay = delay_ms_.load();
            if (delay > 0) std::this_thread::sleep_for(std::chrono::milliseconds(delay));

            const bool close_after = close_each_.load();
            std::string resp = "HTTP/1.1 " + std::to_string(status) + " X\r\n"
                               "Content-Type: application/json\r\n"
                               "Content-Length: " + std::to_string(body.size()) + "\r\n";
            if (close_after) resp += "Connection: close\r\n";
            resp += "\r\n" + body;
            send_all(fd, resp);
            if (close_after) { ::close(fd); return; }
        }
    }

    int listen_fd_ = -1;
    int port_ = 0;
    std::thread acceptor_;
    std::vector<std::thread> workers_;
    mutable std::mutex mu_;
    int status_ = 200;
    std::string body_ = kAllowed;
    std::string last_body_;
    std::atomic<int> delay_ms_{0};
    std::atomic<bool> close_each_{false};
    std::atomic<bool> stopped_{false};
    std::atomic<int> connections_{0};
    std::atomic<int> requests_{0};
};

}  // namespace spine_test

#endif  // SPINE_TESTS_FAKE_SPINE_HPP
```

- [ ] **Step 2: First test through it** — in `tests/test_transport.cpp`:

```cpp
// Transport behaviour against a real local HTTP server: connection reuse,
// stats, circuit breaker, manual halt.

#include <chrono>
#include <string>
#include <thread>
#include <vector>

#include "check.hpp"
#include "fake_spine.hpp"
#include "spine/client.hpp"

namespace {

using spine_test::check;
using spine_test::check_eq;
using spine_test::FakeSpine;

spine::Config config_for(const FakeSpine& server) {
    spine::Config cfg;
    cfg.base_url = server.url();
    cfg.org_key = "spine_test_key";
    cfg.timeout = std::chrono::milliseconds(1000);
    return cfg;
}

spine::Action read_action() {
    spine::Action a;
    a.agent_id = "agent-1";
    a.action_type = "marketdata.read";
    a.target_resource = "us-equities-l1";
    return a;
}

void test_real_verdict_over_http() {
    FakeSpine server;
    const spine::Client client(config_for(server));
    const auto r = client.intercept(read_action());
    check(r.allowed, "a 200 allowed response over real HTTP is allowed");
    check(!r.failed_closed, "and it is a real verdict, not a local one");
    check_eq(r.audit_event_id, "ev-1", "audit_event_id comes back over HTTP");
    check(server.last_body().find("\"action_type\":\"marketdata.read\"") != std::string::npos,
          "the server received the intercept body");
}

}  // namespace

void run_transport_tests() {
    test_real_verdict_over_http();
}
```

- [ ] **Step 3: Build and run** — Expected: `43 checks, 0 failures`.

- [ ] **Step 4: Commit** — `test(cpp): add a loopback fake Spine server for transport tests`

---

### Task 3: Connection pool

**Files:**
- Modify: `sdks/cpp/include/spine/client.hpp`, `sdks/cpp/src/client.cpp`, `sdks/cpp/tests/test_transport.cpp`

**Interfaces:**
- Produces: `Config::reuse_connections` (bool, default true); `namespace detail { struct Shared; }` with `CURL* acquire()`, `void release(CURL*, bool healthy)`; `Client::shared_` (`std::shared_ptr<detail::Shared>`); private `Result Client::perform(const Action&, std::chrono::milliseconds) const` (the existing network body).

- [ ] **Step 1: Failing tests** — add to `test_transport.cpp` and call them from `run_transport_tests`:

```cpp
void test_connections_are_reused() {
    FakeSpine server;
    const spine::Client client(config_for(server));
    for (int i = 0; i < 5; ++i) client.intercept(read_action());
    check(server.requests() == 5, "five requests reached the server");
    check(server.connections() == 1, "five calls reuse a single connection");
}

void test_reuse_can_be_turned_off() {
    FakeSpine server;
    spine::Config cfg = config_for(server);
    cfg.reuse_connections = false;
    const spine::Client client(cfg);
    for (int i = 0; i < 3; ++i) client.intercept(read_action());
    check(server.connections() == 3, "with reuse off, each call opens its own connection");
}

void test_copies_share_the_pool() {
    FakeSpine server;
    const spine::Client a(config_for(server));
    const spine::Client b = a;  // NOLINT: copy is the point
    a.intercept(read_action());
    b.intercept(read_action());
    check(server.connections() == 1, "a copied Client shares its connection pool");
}

void test_server_closing_connections_is_harmless() {
    FakeSpine server;
    server.set_close_each(true);
    const spine::Client client(config_for(server));
    bool all_real = true;
    for (int i = 0; i < 3; ++i) all_real = all_real && !client.intercept(read_action()).failed_closed;
    check(all_real, "a server that closes after each response still gets real verdicts");
    check(server.connections() == 3, "and the client reconnects each time");
}

void test_timeout_then_recovery() {
    FakeSpine server;
    spine::Config cfg = config_for(server);
    cfg.timeout = std::chrono::milliseconds(100);
    const spine::Client client(cfg);
    server.set_delay(std::chrono::milliseconds(300));
    const auto slow = client.intercept(read_action());
    check(slow.failed_closed, "a response slower than the budget fails closed");
    server.set_delay(std::chrono::milliseconds(0));
    const auto next = client.intercept(read_action());
    check(!next.failed_closed && next.allowed, "the next call after a timeout gets a real verdict");
}
```

- [ ] **Step 2: Run** — Expected: FAIL to compile (`reuse_connections` unknown).

- [ ] **Step 3: Header changes** in `client.hpp`: add `#include <memory>`; before `struct Config` add `namespace detail { struct Shared; }`; add to `Config` after `fail_open`:

```cpp
    // Keep connections open between calls instead of paying a TCP handshake
    // per order. Copies of a Client share one pool. Turn off only if your
    // network drops idle connections in ways libcurl cannot detect.
    bool reuse_connections = true;
```

Replace the comment above `class Client` with:

```cpp
// A handle to Spine. Copies share one connection pool (and, below, one set of
// stats and one breaker), so copy freely and use from many threads.
```

and add to the private section:

```cpp
    Result perform(const Action& action, std::chrono::milliseconds budget) const;

    Config config_;
    std::shared_ptr<detail::Shared> shared_;
```

(removing the old lone `Config config_;`).

- [ ] **Step 4: Implementation** in `client.cpp`: add `#include <mutex>` and `#include <vector>`. After the anonymous namespace that defines `write_cb`/`ensure_curl_initialised`, add:

```cpp
namespace detail {

struct Shared {
    explicit Shared(bool reuse_connections) : reuse(reuse_connections) {}
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

    const bool reuse;
    std::mutex pool_mu;
    std::vector<CURL*> pool;
};

}  // namespace detail
```

In the constructor, after trimming the URL: `shared_ = std::make_shared<detail::Shared>(config_.reuse_connections);`

Rename the existing `Client::intercept_with_budget` body to `Result Client::perform(const Action& action, std::chrono::milliseconds budget) const`, with these edits: `CURL* curl = shared_->acquire();` instead of `curl_easy_init()`; add `headers = curl_slist_append(headers, "Expect:");` after the content-type header (stops libcurl waiting for a `100 Continue`); replace `curl_easy_cleanup(curl);` with `shared_->release(curl, rc == CURLE_OK);`. Then:

```cpp
Result Client::intercept_with_budget(const Action& action,
                                     std::chrono::milliseconds budget) const {
    return perform(action, budget);
}
```

- [ ] **Step 5: Build and run** — Expected: all checks pass, no warnings.

- [ ] **Step 6: Commit** — `feat(cpp): reuse connections through a shared handle pool`

---

### Task 4: Stats

**Files:** Modify `client.hpp`, `client.cpp`, `test_transport.cpp`.

**Interfaces:**
- Produces: `struct Stats { std::uint64_t allowed, blocked, flagged, failed_closed, short_circuited; std::array<std::uint64_t, 8> latency_buckets; std::chrono::microseconds max_latency; static constexpr std::array<long long, 7> kBucketUpperMs; }`; `Stats Client::stats() const`; `detail::Shared::count(const Result&)`, `observe(std::chrono::microseconds)`; atomics `failed_closed_n`, `short_circuited_n`.
- Counting rule: every call increments exactly one of `allowed`/`blocked`/`flagged` by its final decision. `failed_closed` counts calls that went to the network and got a local verdict. `short_circuited` counts calls answered locally with no network call. Only network calls are added to latency buckets.

- [ ] **Step 1: Failing tests**

```cpp
std::uint64_t bucket_total(const spine::Stats& s) {
    std::uint64_t n = 0;
    for (auto b : s.latency_buckets) n += b;
    return n;
}

void test_stats_count_decisions_and_latency() {
    FakeSpine server;
    const spine::Client client(config_for(server));
    client.intercept(read_action());
    client.intercept(read_action());
    server.respond(200, spine_test::kBlocked);
    client.intercept(read_action());
    server.respond(200, spine_test::kFlagged);
    client.intercept(read_action());
    server.respond(500, "{}");
    client.intercept(read_action());

    const spine::Stats s = client.stats();
    check(s.allowed == 2, "two allowed");
    check(s.blocked == 2, "two blocked (one by policy, one failed closed)");
    check(s.flagged == 1, "one flagged");
    check(s.failed_closed == 1, "the HTTP 500 counts as failed closed");
    check(s.short_circuited == 0, "nothing short-circuited");
    check(bucket_total(s) == 5, "every network call lands in a latency bucket");
    check(s.max_latency.count() > 0, "max latency is recorded");
}

void test_stats_are_exact_under_concurrency() {
    FakeSpine server;
    const spine::Client client(config_for(server));
    std::vector<std::thread> threads;
    for (int t = 0; t < 8; ++t) {
        threads.emplace_back([&client] {
            for (int i = 0; i < 25; ++i) client.intercept(read_action());
        });
    }
    for (auto& t : threads) t.join();
    const spine::Stats s = client.stats();
    check(s.allowed == 200, "200 concurrent calls from 8 threads are all counted");
    check(server.requests() == 200, "and all reached the server");
    check(server.connections() <= 8, "the pool never needs more connections than threads");
}
```

- [ ] **Step 2: Run** — Expected: FAIL to compile (`Stats` unknown).

- [ ] **Step 3: Header** — add `#include <array>`, `#include <cstdint>`; before `struct Config`:

```cpp
// In-process counters, read with Client::stats(). Cheap enough to leave on.
struct Stats {
    // Inclusive upper bounds of the latency buckets, in milliseconds.
    // latency_buckets has one more slot, for anything slower.
    static constexpr std::array<long long, 7> kBucketUpperMs{{1, 2, 5, 10, 20, 50, 100}};

    // Every call lands in exactly one of these, by its final decision.
    std::uint64_t allowed = 0;
    std::uint64_t blocked = 0;
    std::uint64_t flagged = 0;

    // Went to Spine but the verdict was produced locally (timeout, transport
    // error, bad response). A rising count means the control is degrading.
    std::uint64_t failed_closed = 0;

    // Answered locally with no network call: circuit open, or halted.
    std::uint64_t short_circuited = 0;

    // Network calls only.
    std::array<std::uint64_t, 8> latency_buckets{};
    std::chrono::microseconds max_latency{0};
};
```

In `class Client` public: `Stats stats() const;`

- [ ] **Step 4: Implementation** — add `#include <atomic>` and `#include <array>`. In `detail::Shared` constructor body: `for (auto& b : buckets) b.store(0);`. Add members and helpers:

```cpp
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

    std::atomic<std::uint64_t> allowed{0};
    std::atomic<std::uint64_t> blocked{0};
    std::atomic<std::uint64_t> flagged{0};
    std::atomic<std::uint64_t> failed_closed_n{0};
    std::atomic<std::uint64_t> short_circuited_n{0};
    std::array<std::atomic<std::uint64_t>, 8> buckets;
    std::atomic<long long> max_latency_us{0};
```

Replace `intercept_with_budget`:

```cpp
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
```

- [ ] **Step 5: Build and run** — Expected: all pass.

- [ ] **Step 6: Commit** — `feat(cpp): count decisions and latency in-process with Client::stats()`

---

### Task 5: Circuit breaker

**Files:** Modify `client.hpp`, `client.cpp`, `test_transport.cpp`.

**Interfaces:**
- Produces: `enum class BreakerState { Closed, Open, HalfOpen }`; `const char* to_string(BreakerState)`; `Config::breaker_threshold` (int, 5), `Config::breaker_cooldown` (`std::chrono::milliseconds`, 5000), `Config::on_breaker_change` (`std::function<void(BreakerState)>`); `BreakerState Client::breaker_state() const`; `detail::Shared::admit`, `settle`; free helper `Result local_verdict(bool allow, std::string reason, steady_clock::time_point started)` in `client.cpp`'s anonymous namespace.

- [ ] **Step 1: Failing tests**

```cpp
void test_breaker_trips_short_circuits_and_recovers() {
    FakeSpine server;
    server.respond(500, "{}");
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 3;
    cfg.breaker_cooldown = std::chrono::milliseconds(100);
    std::vector<spine::BreakerState> seen;
    cfg.on_breaker_change = [&seen](spine::BreakerState s) { seen.push_back(s); };
    const spine::Client client(cfg);

    for (int i = 0; i < 3; ++i) client.intercept(read_action());
    check(client.breaker_state() == spine::BreakerState::Open, "three failures in a row open the breaker");
    check(server.requests() == 3, "the failures did reach the server");

    const auto refused = client.intercept(read_action());
    check(!refused.allowed && refused.failed_closed, "an open breaker refuses locally");
    check_eq(refused.reason, "circuit open", "with a clear reason");
    check(server.requests() == 3, "and makes no network call");
    check(client.stats().short_circuited == 1, "the refusal counts as short-circuited");

    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    server.respond(200, spine_test::kAllowed);
    const auto probe = client.intercept(read_action());
    check(probe.allowed && !probe.failed_closed, "after the cooldown, a probe reaches Spine");
    check(client.breaker_state() == spine::BreakerState::Closed, "a real verdict closes the breaker");
    check(seen.size() == 3 && seen[0] == spine::BreakerState::Open &&
              seen[1] == spine::BreakerState::HalfOpen && seen[2] == spine::BreakerState::Closed,
          "the callback saw Open, HalfOpen, Closed");
}

void test_failed_probe_reopens() {
    FakeSpine server;
    server.respond(500, "{}");
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 1;
    cfg.breaker_cooldown = std::chrono::milliseconds(50);
    const spine::Client client(cfg);
    client.intercept(read_action());
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    client.intercept(read_action());  // the probe, still failing
    check(client.breaker_state() == spine::BreakerState::Open, "a failed probe re-opens the breaker");
    check(server.requests() == 2, "exactly one probe was sent");
}

void test_success_resets_the_failure_count() {
    FakeSpine server;
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 2;
    const spine::Client client(cfg);
    server.respond(500, "{}");
    client.intercept(read_action());
    server.respond(200, spine_test::kAllowed);
    client.intercept(read_action());
    server.respond(500, "{}");
    client.intercept(read_action());
    check(client.breaker_state() == spine::BreakerState::Closed,
          "failures separated by a real verdict do not trip the breaker");
}

void test_open_breaker_honours_fail_open() {
    FakeSpine server;
    server.respond(500, "{}");
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 1;
    cfg.fail_open = true;
    const spine::Client client(cfg);
    client.intercept(read_action());
    const auto r = client.intercept(read_action());
    check(r.allowed && r.failed_closed, "with fail_open, an open breaker allows locally");
}

void test_threshold_zero_disables_the_breaker() {
    FakeSpine server;
    server.respond(500, "{}");
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 0;
    const spine::Client client(cfg);
    for (int i = 0; i < 10; ++i) client.intercept(read_action());
    check(client.breaker_state() == spine::BreakerState::Closed, "threshold 0 never trips");
    check(server.requests() == 10, "every call still goes to Spine");
}

void test_throwing_callback_is_contained() {
    FakeSpine server;
    server.respond(500, "{}");
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 1;
    cfg.on_breaker_change = [](spine::BreakerState) { throw 42; };
    const spine::Client client(cfg);
    bool threw = false;
    try {
        client.intercept(read_action());
    } catch (...) {
        threw = true;
    }
    check(!threw, "a callback that throws does not escape intercept()");
    check(client.breaker_state() == spine::BreakerState::Open, "and the breaker still tripped");
}
```

Also in `test_client.cpp` `test_response_parsing`, nothing changes. In `test_fails_closed_when_spine_is_unreachable` nothing changes (one failure < threshold 5).

- [ ] **Step 2: Run** — Expected: FAIL to compile.

- [ ] **Step 3: Header** — add `#include <functional>`. Before `struct Config`:

```cpp
enum class BreakerState {
    Closed,    // normal: every call goes to Spine
    Open,      // Spine keeps failing: answer locally, no network call
    HalfOpen,  // cooldown over: one probe is in flight
};

const char* to_string(BreakerState s);
```

Add to `Config`:

```cpp
    // Consecutive locally-produced verdicts that open the circuit breaker.
    // While open, calls are answered locally at once instead of each one
    // waiting out the timeout. 0 disables the breaker.
    int breaker_threshold = 5;

    // How long the breaker stays open before one probe is let through.
    std::chrono::milliseconds breaker_cooldown{5000};

    // Optional. Called on the calling thread after each breaker transition;
    // wire it to your alerting. Exceptions it throws are swallowed.
    std::function<void(BreakerState)> on_breaker_change;
```

In `class Client` public: `BreakerState breaker_state() const;`

- [ ] **Step 4: Implementation** — in the anonymous namespace (after `ensure_curl_initialised`):

```cpp
using Clock = std::chrono::steady_clock;

Result local_verdict(bool allow, std::string reason, Clock::time_point started) {
    Result r;
    r.allowed = allow;
    r.decision = allow ? Decision::Allowed : Decision::Blocked;
    r.reason = std::move(reason);
    r.failed_closed = true;
    r.latency = std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - started);
    return r;
}

void notify(const Config& c, BreakerState s) {
    if (!c.on_breaker_change) return;
    try {
        c.on_breaker_change(s);
    } catch (...) {
        // Never throw on the order path, including from user code.
    }
}
```

`to_string(BreakerState)` next to `to_string(Decision)`:

```cpp
const char* to_string(BreakerState s) {
    switch (s) {
        case BreakerState::Open: return "open";
        case BreakerState::HalfOpen: return "half-open";
        case BreakerState::Closed: break;
    }
    return "closed";
}
```

In `detail::Shared` add (note: `Clock` must be visible, so declare `using Clock = std::chrono::steady_clock;` at the top of `namespace detail` too):

```cpp
    enum class Admission { Pass, Probe, ShortCircuit };

    Admission admit(const Config& c, bool* changed) {
        *changed = false;
        if (c.breaker_threshold <= 0) return Admission::Pass;
        std::lock_guard<std::mutex> g(breaker_mu);
        switch (state) {
            case BreakerState::Closed:
                return Admission::Pass;
            case BreakerState::Open:
                if (Clock::now() - opened_at >= c.breaker_cooldown) {
                    state = BreakerState::HalfOpen;
                    *changed = true;
                    return Admission::Probe;
                }
                return Admission::ShortCircuit;
            case BreakerState::HalfOpen:
                break;
        }
        return Admission::ShortCircuit;  // a probe is already in flight
    }

    // Returns true and sets *now if the state changed.
    bool settle(const Config& c, bool local, bool probe, BreakerState* now) {
        if (c.breaker_threshold <= 0) return false;
        std::lock_guard<std::mutex> g(breaker_mu);
        const BreakerState before = state;
        if (!local) {
            failures = 0;
            state = BreakerState::Closed;
        } else {
            ++failures;
            if (probe || (state == BreakerState::Closed && failures >= c.breaker_threshold)) {
                state = BreakerState::Open;
                opened_at = Clock::now();
            }
        }
        *now = state;
        return state != before;
    }

    BreakerState breaker() {
        std::lock_guard<std::mutex> g(breaker_mu);
        return state;
    }

    std::mutex breaker_mu;
    BreakerState state = BreakerState::Closed;
    int failures = 0;
    Clock::time_point opened_at{};
```

Replace `intercept_with_budget`:

```cpp
Result Client::intercept_with_budget(const Action& action,
                                     std::chrono::milliseconds budget) const {
    const auto started = Clock::now();

    bool changed = false;
    const auto admission = shared_->admit(config_, &changed);
    if (changed) notify(config_, BreakerState::HalfOpen);
    if (admission == detail::Shared::Admission::ShortCircuit) {
        Result r = local_verdict(config_.fail_open, "circuit open", started);
        ++shared_->short_circuited_n;
        shared_->count(r);
        return r;
    }

    Result r = perform(action, budget);

    BreakerState now = BreakerState::Closed;
    if (shared_->settle(config_, r.failed_closed,
                        admission == detail::Shared::Admission::Probe, &now)) {
        notify(config_, now);
    }

    if (r.failed_closed) ++shared_->failed_closed_n;
    shared_->observe(r.latency);
    shared_->count(r);
    return r;
}

BreakerState Client::breaker_state() const { return shared_->breaker(); }
```

- [ ] **Step 5: Build and run** — Expected: all pass, including the existing unreachable-port tests.

- [ ] **Step 6: Commit** — `feat(cpp): add a circuit breaker that answers locally while Spine is failing`

---

### Task 6: Manual halt and risk-reducing calls

**Files:** Modify `client.hpp`, `client.cpp`, `test_transport.cpp`.

**Interfaces:**
- Produces: `void Client::halt(const std::string& reason) const`, `void Client::resume() const`, `bool Client::halted() const`, `Result Client::intercept_risk_reducing(const Action&) const`; private `Result Client::intercept_impl(const Action&, std::chrono::milliseconds, bool risk_reducing) const`.
- Local-verdict reasons: `halted: <reason>`, `circuit open`, or the transport reason (`latency budget exceeded`, `transport error: ...`, `Spine returned HTTP <n>`, `unparseable response from Spine`).

- [ ] **Step 1: Failing tests**

```cpp
void test_halt_blocks_without_a_network_call() {
    FakeSpine server;
    spine::Config cfg = config_for(server);
    cfg.fail_open = true;  // a halt must win even over fail_open
    const spine::Client client(cfg);
    client.halt("desk kill switch");
    check(client.halted(), "halted() reports the halt");
    const auto r = client.intercept(read_action());
    check(!r.allowed && r.failed_closed, "a halted client refuses locally");
    check_eq(r.reason, "halted: desk kill switch", "the reason carries the operator's text");
    check(server.requests() == 0, "and makes no network call");

    client.resume();
    check(!client.halted(), "resume() clears the halt");
    check(client.intercept(read_action()).allowed, "after resume, calls reach Spine again");
}

void test_risk_reducing_calls_pass_a_halt_and_an_open_breaker() {
    FakeSpine server;
    server.respond(500, "{}");
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 1;
    const spine::Client client(cfg);

    const auto failed = client.intercept_risk_reducing(read_action());
    check(failed.allowed && failed.failed_closed, "a risk-reducing call is allowed when Spine fails");
    check(client.breaker_state() == spine::BreakerState::Open, "the failure still counts toward the breaker");

    const auto open = client.intercept_risk_reducing(read_action());
    check(open.allowed && open.failed_closed, "and allowed while the breaker is open");
    check_eq(open.reason, "circuit open", "with the breaker as the reason");

    client.halt("test");
    const auto halted = client.intercept_risk_reducing(read_action());
    check(halted.allowed, "and allowed during a halt");
}

void test_real_block_of_a_risk_reducing_call_is_respected() {
    FakeSpine server;
    server.respond(200, spine_test::kBlocked);
    const spine::Client client(config_for(server));
    const auto r = client.intercept_risk_reducing(read_action());
    check(!r.allowed && !r.failed_closed, "a deliberate block from Spine is not overridden");
}
```

- [ ] **Step 2: Run** — Expected: FAIL to compile.

- [ ] **Step 3: Header** — `class Client` public:

```cpp
    // For actions that reduce risk, such as cancelling an order. Asks Spine
    // as usual and honours a real verdict, including a deliberate block. But
    // when the verdict would be produced locally — Spine unreachable, budget
    // exceeded, breaker open, halted — it allows, because refusing a cancel
    // keeps the risk on. Result::failed_closed still marks it as local.
    Result intercept_risk_reducing(const Action& action) const;

    // Local kill switch. While halted, every call except risk-reducing ones
    // is refused at once, with no network call, regardless of fail_open.
    // Copies of this Client share the halt.
    void halt(const std::string& reason) const;
    void resume() const;
    bool halted() const;
```

private: `Result intercept_impl(const Action& action, std::chrono::milliseconds budget, bool risk_reducing) const;`

- [ ] **Step 4: Implementation** — in `detail::Shared`:

```cpp
    bool is_halted(std::string* reason) {
        std::lock_guard<std::mutex> g(halt_mu);
        if (halted && reason != nullptr) *reason = halt_reason;
        return halted;
    }

    std::mutex halt_mu;
    bool halted = false;
    std::string halt_reason;
```

Rename the Task 5 body of `intercept_with_budget` to `intercept_impl(const Action& action, std::chrono::milliseconds budget, bool risk_reducing) const` and change it to:

```cpp
Result Client::intercept_impl(const Action& action, std::chrono::milliseconds budget,
                              bool risk_reducing) const {
    const auto started = Clock::now();

    std::string halt_reason;
    if (shared_->is_halted(&halt_reason)) {
        // fail_open deliberately does not apply: a halt is a decision.
        Result r = local_verdict(risk_reducing, "halted: " + halt_reason, started);
        ++shared_->short_circuited_n;
        shared_->count(r);
        return r;
    }

    bool changed = false;
    const auto admission = shared_->admit(config_, &changed);
    if (changed) notify(config_, BreakerState::HalfOpen);
    if (admission == detail::Shared::Admission::ShortCircuit) {
        Result r = local_verdict(risk_reducing || config_.fail_open, "circuit open", started);
        ++shared_->short_circuited_n;
        shared_->count(r);
        return r;
    }

    Result r = perform(action, budget);

    BreakerState now = BreakerState::Closed;
    if (shared_->settle(config_, r.failed_closed,
                        admission == detail::Shared::Admission::Probe, &now)) {
        notify(config_, now);
    }

    if (r.failed_closed) {
        ++shared_->failed_closed_n;
        if (risk_reducing) {
            r.allowed = true;
            r.decision = Decision::Allowed;
        }
    }
    shared_->observe(r.latency);
    shared_->count(r);
    return r;
}

Result Client::intercept_with_budget(const Action& action,
                                     std::chrono::milliseconds budget) const {
    return intercept_impl(action, budget, false);
}

Result Client::intercept_risk_reducing(const Action& action) const {
    return intercept_impl(action, config_.timeout, true);
}

void Client::halt(const std::string& reason) const {
    std::lock_guard<std::mutex> g(shared_->halt_mu);
    shared_->halted = true;
    shared_->halt_reason = reason;
}

void Client::resume() const {
    std::lock_guard<std::mutex> g(shared_->halt_mu);
    shared_->halted = false;
    shared_->halt_reason.clear();
}

bool Client::halted() const { return shared_->is_halted(nullptr); }
```

- [ ] **Step 5: Build and run** — Expected: all pass.

- [ ] **Step 6: Commit** — `feat(cpp): add a manual halt and fail-open-for-cancels risk-reducing calls`

---

### Task 7: Reference pricing for market orders

**Files:** Modify `finance.hpp`, `finance.cpp`, `tests/test_lifecycle.cpp`.

**Interfaces:**
- Produces: `Order::client_order_id` (string) and `Order::reference_price` (double, 0) appended after `trader_id`; `NotionalBands::market_collar` (double, 1.05); `enum class Pricing { Limit, Reference, Unpriced }`; `const char* to_string(Pricing)` returning `"limit"`, `"reference"`, `"unpriced"`; `Pricing pricing_of(const Order&)`; `double notional(const Order&, const NotionalBands&)`; `std::string action_type_for(const std::string& prefix, const Order&, const NotionalBands&)`. Existing `notional(const Order&)` = `notional(o, NotionalBands{})`; existing `action_type_for(o, bands)` = `action_type_for("order.place", o, bands)`.

- [ ] **Step 1: Failing tests** — `tests/test_lifecycle.cpp`:

```cpp
// Order lifecycle: pricing, amends, cancels.

#include <string>

#include "check.hpp"
#include "fake_spine.hpp"
#include "spine/finance.hpp"

namespace {

using spine_test::check;
using spine_test::check_eq;
using namespace spine::finance;

Order base_order() {
    Order o{"AAPL", Side::Buy, 100, 150.0, "XNAS", "stat-arb", "t-1"};
    o.client_order_id = "co-1";
    return o;
}

void test_market_order_priced_off_reference() {
    NotionalBands bands;  // collar 1.05
    Order o = base_order();
    o.limit_price = 0.0;
    o.reference_price = 200.0;
    check(pricing_of(o) == Pricing::Reference, "a market order with a reference price is reference-priced");
    check(notional(o, bands) == 100 * 200.0 * 1.05, "notional applies the collar");
    check_eq(action_type_for(o, bands), "order.place", "a small reference-priced order is ordinary");

    o.quantity = 10000;  // 2.1M with collar
    check_eq(action_type_for(o, bands), "order.place.large", "a big one goes to review");
}

void test_collar_can_push_an_order_over_a_band() {
    NotionalBands bands;
    Order o = base_order();
    o.limit_price = 0.0;
    o.reference_price = 100.0;
    o.quantity = 9600;  // 960k raw, 1.008M with the 5% collar
    check_eq(action_type_for(o, bands), "order.place.large",
             "the collar is what moves a near-threshold market order into review");
}

void test_limit_price_wins_over_reference() {
    NotionalBands bands;
    Order o = base_order();
    o.reference_price = 9999.0;
    check(pricing_of(o) == Pricing::Limit, "a limit order is limit-priced even with a reference");
    check(notional(o, bands) == 15000.0, "and its notional ignores the reference");
}

void test_unpriced_without_reference_is_unchanged() {
    NotionalBands bands;
    Order o = base_order();
    o.limit_price = 0.0;
    check(pricing_of(o) == Pricing::Unpriced, "no limit and no reference is unpriced");
    check_eq(action_type_for(o, bands), "order.place.unpriced", "still routed for review");
    check(notional(o) == 0.0, "the one-argument notional() is unchanged");
}

void test_prefix_overload() {
    NotionalBands bands;
    Order o = base_order();
    check_eq(action_type_for("order.amend", o, bands), "order.amend", "prefix overload, ordinary");
    o.quantity = 100000;
    check_eq(action_type_for("order.amend", o, bands), "order.amend.block", "prefix overload, block band");
}

}  // namespace

void run_lifecycle_tests() {
    test_market_order_priced_off_reference();
    test_collar_can_push_an_order_over_a_band();
    test_limit_price_wins_over_reference();
    test_unpriced_without_reference_is_unchanged();
    test_prefix_overload();
}
```

- [ ] **Step 2: Run** — Expected: FAIL to compile.

- [ ] **Step 3: Header** — in `struct Order`, after `trader_id`:

```cpp
    // Your own order identifier, so amends and cancels can be tied back to
    // the order in the audit trail.
    std::string client_order_id;

    // For market orders: the price you would mark this at (last trade, mid).
    // With it, a market order is sized like a limit order, plus the collar.
    // You own its freshness; a stale reference is a wrong notional.
    double reference_price = 0.0;
```

In `NotionalBands`, after `refuse_at`:

```cpp
    // Market orders priced off a reference are marked up by this factor, to
    // cover slippage between the reference and the fill.
    double market_collar = 1.05;
```

After `NotionalBands`:

```cpp
enum class Pricing { Limit, Reference, Unpriced };

const char* to_string(Pricing p);

// Limit if there is a limit price; Reference if only a reference price;
// otherwise Unpriced.
Pricing pricing_of(const Order& o);

// Notional under these bands' collar. 0 when Unpriced.
double notional(const Order& o, const NotionalBands& bands);

// Band selection under any action-type prefix: prefix, prefix.large,
// prefix.block, prefix.unpriced.
std::string action_type_for(const std::string& prefix, const Order& o,
                            const NotionalBands& bands);
```

Keep the existing `notional(const Order&)` and `action_type_for(const Order&, const NotionalBands&)` declarations and update their comments to say they are the `order.place` / default-bands forms.

- [ ] **Step 4: Implementation** in `finance.cpp`:

```cpp
const char* to_string(Pricing p) {
    switch (p) {
        case Pricing::Limit: return "limit";
        case Pricing::Reference: return "reference";
        case Pricing::Unpriced: break;
    }
    return "unpriced";
}

Pricing pricing_of(const Order& o) {
    if (o.limit_price > 0.0) return Pricing::Limit;
    if (o.reference_price > 0.0) return Pricing::Reference;
    return Pricing::Unpriced;
}

double notional(const Order& o, const NotionalBands& bands) {
    const double qty = static_cast<double>(o.quantity);
    switch (pricing_of(o)) {
        case Pricing::Limit: return qty * o.limit_price;
        case Pricing::Reference: return qty * o.reference_price * bands.market_collar;
        case Pricing::Unpriced: break;
    }
    return 0.0;
}

double notional(const Order& o) { return notional(o, NotionalBands{}); }

std::string action_type_for(const std::string& prefix, const Order& o,
                            const NotionalBands& bands) {
    // Unknown notional must not silently mean "small": an unbounded order is
    // exactly the one a human should see.
    if (pricing_of(o) == Pricing::Unpriced) return prefix + ".unpriced";
    const double value = notional(o, bands);
    if (value >= bands.refuse_at) return prefix + ".block";
    if (value >= bands.review_at) return prefix + ".large";
    return prefix;
}

std::string action_type_for(const Order& o, const NotionalBands& bands) {
    return action_type_for("order.place", o, bands);
}
```

replacing the old `notional` and `action_type_for` definitions.

- [ ] **Step 5: Build and run** — Expected: all pass, including the original banding tests.

- [ ] **Step 6: Commit** — `feat(cpp): price market orders off a reference price with a slippage collar`

---

### Task 8: Order metadata and amends

**Files:** Modify `finance.hpp`, `finance.cpp`, `tests/test_lifecycle.cpp`.

**Interfaces:**
- Consumes: `Client`, `pricing_of`, `notional(o, bands)`, `action_type_for(prefix, ...)`.
- Produces: `Result OrderGate::check_amend(const Order& original, const Order& amended, const std::string& session_id = "") const`; `const Client& OrderGate::client() const`. Order metadata keys, in order: `side`, `quantity`, `limit_price`, `notional`, `pricing`, `venue`, `strategy_id`, `trader_id`, `client_order_id`, then `reference_price` only when reference-priced. Amends append `prev_quantity`, `prev_limit_price`, `prev_notional`.

- [ ] **Step 1: Failing tests** — add to `test_lifecycle.cpp`:

```cpp
spine::Config cfg_for(const spine_test::FakeSpine& s) {
    spine::Config cfg;
    cfg.base_url = s.url();
    cfg.org_key = "spine_test_key";
    cfg.timeout = std::chrono::milliseconds(1000);
    return cfg;
}

bool has(const std::string& body, const std::string& fragment) {
    return body.find(fragment) != std::string::npos;
}

void test_check_sends_full_order_metadata() {
    spine_test::FakeSpine server;
    OrderGate gate(spine::Client(cfg_for(server)), "agent-1");
    Order o = base_order();
    o.limit_price = 0.0;
    o.reference_price = 200.0;
    gate.check(o);
    const std::string body = server.last_body();
    check(has(body, "\"client_order_id\":\"co-1\""), "check sends client_order_id");
    check(has(body, "\"pricing\":\"reference\""), "check sends how the order was priced");
    check(has(body, "\"reference_price\":\"200.00\""), "and the reference price used");
    check(has(body, "\"notional\":\"21000.00\""), "notional includes the collar");
}

void test_amend_bands_on_the_new_size() {
    spine_test::FakeSpine server;
    OrderGate gate(spine::Client(cfg_for(server)), "agent-1");
    Order before = base_order();          // 15k
    Order after = base_order();
    after.quantity = 20000;               // 3M
    const auto r = gate.check_amend(before, after);
    check(r.allowed && !r.failed_closed, "an amend gets a real verdict");
    const std::string body = server.last_body();
    check(has(body, "\"action_type\":\"order.amend.large\""), "an amend is banded on its new notional");
    check(has(body, "\"quantity\":\"20000\""), "the new quantity is sent");
    check(has(body, "\"prev_quantity\":\"100\""), "and the old one");
    check(has(body, "\"prev_notional\":\"15000.00\""), "and the old notional");
    check(has(body, "\"target_resource\":\"AAPL\""), "the target is the symbol");
}

void test_amend_fails_closed_like_a_new_order() {
    spine::Config cfg;
    cfg.base_url = "http://127.0.0.1:1";
    cfg.org_key = "spine_test_key";
    cfg.timeout = std::chrono::milliseconds(250);
    OrderGate gate(spine::Client(cfg), "agent-1");
    const auto r = gate.check_amend(base_order(), base_order());
    check(!r.allowed && r.failed_closed, "an amend that cannot reach Spine is refused");
}
```

and register them in `run_lifecycle_tests`. Add `#include <chrono>` at the top.

- [ ] **Step 2: Run** — Expected: FAIL to compile (`check_amend`).

- [ ] **Step 3: Header** — in `class OrderGate` public:

```cpp
    // An amend is a new risk decision at the new size, so it is banded on
    // the amended order's notional: order.amend, .large, .block, .unpriced.
    // The previous size and price travel with it for the audit trail.
    Result check_amend(const Order& original, const Order& amended,
                       const std::string& session_id = "") const;

    // The underlying client, for stats(), breaker_state(), and halt().
    const Client& client() const { return client_; }
```

- [ ] **Step 4: Implementation** — in `finance.cpp` anonymous namespace (after `format_number`), add `#include <vector>` at the top of the file:

```cpp
using Metadata = std::vector<std::pair<std::string, std::string>>;

// Everything here lands verbatim in the hash-chained audit log, which is the
// point: it is the record of what was asked and on whose behalf. Keep it to
// trade facts — no client identifiers, research, or rationale text.
Metadata order_metadata(const Order& o, const NotionalBands& bands) {
    const Pricing pricing = pricing_of(o);
    Metadata md = {
        {"side", to_string(o.side)},
        {"quantity", std::to_string(o.quantity)},
        {"limit_price", format_number(o.limit_price)},
        {"notional", format_number(notional(o, bands))},
        {"pricing", to_string(pricing)},
        {"venue", o.venue},
        {"strategy_id", o.strategy_id},
        {"trader_id", o.trader_id},
        {"client_order_id", o.client_order_id},
    };
    if (pricing == Pricing::Reference) {
        md.emplace_back("reference_price", format_number(o.reference_price));
    }
    return md;
}
```

Change `OrderGate::check` to build `action.action_type = action_type_for(order, bands_);` and `action.metadata = order_metadata(order, bands_);` (deleting the old inline metadata list and its comment, which now lives above `order_metadata`). Add:

```cpp
Result OrderGate::check_amend(const Order& original, const Order& amended,
                              const std::string& session_id) const {
    Action action;
    action.agent_id = agent_id_;
    action.action_type = action_type_for("order.amend", amended, bands_);
    action.target_resource = amended.symbol;
    action.session_id = session_id;
    action.metadata = order_metadata(amended, bands_);
    action.metadata.emplace_back("prev_quantity", std::to_string(original.quantity));
    action.metadata.emplace_back("prev_limit_price", format_number(original.limit_price));
    action.metadata.emplace_back("prev_notional", format_number(notional(original, bands_)));
    return client_.intercept(action);
}
```

- [ ] **Step 5: Build and run** — Expected: all pass.

- [ ] **Step 6: Commit** — `feat(cpp): check order amends, banded on the amended notional`

---

### Task 9: Cancels

**Files:** Modify `finance.hpp`, `finance.cpp`, `tests/test_lifecycle.cpp`.

**Interfaces:**
- Consumes: `Client::intercept_risk_reducing`.
- Produces: `Result OrderGate::check_cancel(const Order& order, const std::string& session_id = "") const`. Metadata: `client_order_id`, `side`, `quantity`, `strategy_id`, `trader_id`. On a local verdict, reason becomes `cancel allowed locally: <reason>`.

- [ ] **Step 1: Failing tests**

```cpp
void test_cancel_is_allowed_when_spine_is_unreachable() {
    spine::Config cfg;
    cfg.base_url = "http://127.0.0.1:1";
    cfg.org_key = "spine_test_key";
    cfg.timeout = std::chrono::milliseconds(250);
    OrderGate gate(spine::Client(cfg), "agent-1");
    const auto r = gate.check_cancel(base_order());
    check(r.allowed, "a cancel goes through when Spine is unreachable");
    check(r.failed_closed, "but is marked as decided locally");
    check(r.reason.rfind("cancel allowed locally: ", 0) == 0, "with a reason that says so");
}

void test_cancel_passes_a_halt_but_orders_do_not() {
    spine_test::FakeSpine server;
    OrderGate gate(spine::Client(cfg_for(server)), "agent-1");
    gate.client().halt("risk breach");
    check(!gate.check(base_order()).allowed, "a halt stops new orders");
    const auto cancel = gate.check_cancel(base_order());
    check(cancel.allowed, "a halt does not stop cancels");
    check_eq(cancel.reason, "cancel allowed locally: halted: risk breach", "and says why");
    check(server.requests() == 0, "neither made a network call during the halt");
}

void test_cancel_sends_its_facts_and_respects_a_real_block() {
    spine_test::FakeSpine server;
    OrderGate gate(spine::Client(cfg_for(server)), "agent-1");
    gate.check_cancel(base_order());
    const std::string body = server.last_body();
    check(has(body, "\"action_type\":\"order.cancel\""), "a cancel is order.cancel");
    check(has(body, "\"client_order_id\":\"co-1\""), "and names the order it cancels");

    server.respond(200, spine_test::kBlocked);
    const auto r = gate.check_cancel(base_order());
    check(!r.allowed && !r.failed_closed, "a deliberate block of a cancel by policy stands");
}
```

Register them in `run_lifecycle_tests`.

- [ ] **Step 2: Run** — Expected: FAIL to compile.

- [ ] **Step 3: Header** — in `class OrderGate` public:

```cpp
    // Cancelling reduces risk, so this one does not fail closed. Spine is
    // asked as usual and a real verdict stands, including a deliberate
    // block. But if the answer would be produced locally — Spine
    // unreachable, budget exceeded, breaker open, halted — the cancel is
    // allowed, with failed_closed set and the reason prefixed
    // "cancel allowed locally: ". Refusing a cancel keeps the position on.
    Result check_cancel(const Order& order, const std::string& session_id = "") const;
```

- [ ] **Step 4: Implementation**

```cpp
Result OrderGate::check_cancel(const Order& order, const std::string& session_id) const {
    Action action;
    action.agent_id = agent_id_;
    action.action_type = "order.cancel";
    action.target_resource = order.symbol;
    action.session_id = session_id;
    action.metadata = {
        {"client_order_id", order.client_order_id},
        {"side", to_string(order.side)},
        {"quantity", std::to_string(order.quantity)},
        {"strategy_id", order.strategy_id},
        {"trader_id", order.trader_id},
    };
    Result r = client_.intercept_risk_reducing(action);
    if (r.failed_closed) r.reason = "cancel allowed locally: " + r.reason;
    return r;
}
```

- [ ] **Step 5: Build and run** — Expected: all pass.

- [ ] **Step 6: Commit** — `feat(cpp): check cancels, allowing them when the verdict would be local`

---

### Task 10: Policy pack

**Files:** Modify `examples/finance/seed_finance_policies.py`.

- [ ] **Step 1: Read the file** (`sed -n 55,233p examples/finance/seed_finance_policies.py`) to see the policy list and the mandate's expected action types.

- [ ] **Step 2: Edit `policies()`**:
  - Add `"order.amend"`, `"order.amend.large"`, `"order.amend.unpriced"`, `"order.amend.block"` to the restricted-list deny's `action_types`.
  - Add `"order.amend.block"` to the notional-ceiling deny's `action_types`.
  - Add `"order.amend.large"` to the large-notional flag and `"order.amend.unpriced"` to the unpriced flag.
  - Add `"order.amend"` to the trading-hours allow for `order.place`.
  - Add a new policy after the trading-hours allow:

```python
        # Cancels reduce risk. Allowed at any hour, in any name: a restricted
        # or out-of-hours cancel is still a cancel you want to go through.
        {
            "name": "Cancels: always allowed",
            "rule_type": "action",
            "rule_config": {"effect": "allow", "action_types": ["order.cancel"]},
        },
```

  - Add the four `order.amend*` types and `"order.cancel"` to the mandate session's expected action types / constraints list, matching its existing format.
  - Update the module docstring's first paragraph to mention amends and cancels.

- [ ] **Step 3: Verify** — `python3 -c "import ast,sys; ast.parse(open('examples/finance/seed_finance_policies.py').read())"` and `.venv/bin/ruff check examples && .venv/bin/ruff format --check examples` from the repo root. If Docker is available, `make demo` then run the script and `./sdks/cpp/build/pretrade_gate`.

- [ ] **Step 4: Commit** — `feat(examples): cover amends and cancels in the finance policy pack`

---

### Task 11: Example program

**Files:** Modify `sdks/cpp/examples/pretrade_gate.cpp`.

- [ ] **Step 1: Extend the example** — after the order loop and before the summary:
  - Give each case order a `client_order_id` (`"co-1"`…`"co-4"`); add a fifth case, a market order with `reference_price = 450.0`, labelled `"market order priced off a reference — sized, not guessed"`.
  - Print an amend of the first order to 20 000 shares (`gate.check_amend`), labelled `"amend: resize the first order to 20,000 shares"`, using `print_verdict` with `action_type_for("order.amend", ...)`.
  - Print a cancel of the first order (`gate.check_cancel`), labelled `"cancel: always allowed if Spine cannot answer"`.
  - Print the stats block:

```cpp
    const spine::Stats s = gate.client().stats();
    std::printf("  stats: %llu allowed, %llu blocked, %llu flagged, "
                "%llu failed closed, %llu short-circuited, max %lld us\n",
                static_cast<unsigned long long>(s.allowed),
                static_cast<unsigned long long>(s.blocked),
                static_cast<unsigned long long>(s.flagged),
                static_cast<unsigned long long>(s.failed_closed),
                static_cast<unsigned long long>(s.short_circuited),
                static_cast<long long>(s.max_latency.count()));
    std::printf("  breaker: %s\n\n", spine::to_string(gate.client().breaker_state()));
```

  - `print_verdict` prints `order.limit_price` — leave it, and add `order.reference_price` when `limit_price` is 0 and `reference_price` > 0.

- [ ] **Step 2: Build and run without Spine** — `cmake --build build -j4 && ./build/pretrade_gate`. Expected: every order STOPPED with `failed closed`, the cancel SENT with `cancel allowed locally`, the breaker `open` after five failures with later calls short-circuited, exit 0.

- [ ] **Step 3: Commit** — `docs(cpp): show amends, cancels, stats, and the breaker in the example`

---

### Task 12: Documentation

**Files:** Modify `sdks/cpp/README.md`, `CHANGELOG.md`.

- [ ] **Step 1: README**
  - "Using it": add `client_order_id` to the order, plus short `check_amend`, `check_cancel`, `client().stats()`, `client().halt()` snippets.
  - New section **"Amends, cancels, and market orders"**: amend banding table (`order.amend*`), why cancels fail open, reference pricing with the collar and the stale-reference warning.
  - New section **"When Spine degrades"**: breaker states and defaults, `on_breaker_change`, stats fields and the counting rule, halt vs breaker.
  - Replace the "Connection pooling" bullet under "What this does not do" with a short "Connections" note: pooled by default, copies share it, `reuse_connections = false` to opt out.
  - Keep all existing measured numbers; do not invent new ones.

- [ ] **Step 2: CHANGELOG** — under `### Added`: one bullet each for connection reuse, amends/cancels/reference pricing, stats + breaker + halt.

- [ ] **Step 3: Commit** — `docs(cpp): document pooling, the order lifecycle, and degradation handling`

---

## Final verification

From the repo root: `make test-cpp` (or the build/test commands above) shows 0 failures and no warnings; `make test` still passes; push and confirm the CI "C++ SDK build and tests" job is green.
