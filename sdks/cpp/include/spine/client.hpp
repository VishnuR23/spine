// Spine C++ client.
//
// Exists because the systems that most need a pre-trade gate — order
// management, execution, and smart-order-routing stacks — are written in C++
// and cannot afford a Python bridge on the order path.
//
// Two properties drive the whole design:
//
//   1. A latency budget, not a timeout. An order gate that occasionally takes
//      two seconds is worse than one that reliably takes 50 ms and refuses,
//      because the trading system's own deadline is fixed. `timeout` here is
//      a hard ceiling on the call.
//
//   2. Fail closed. If Spine cannot be reached, or the budget is exceeded,
//      the default answer is "do not send the order". A control that
//      disappears under load is not a control. Set `fail_open` only if you
//      have decided, explicitly, that unsupervised trading is the safer
//      failure — it usually is not.
//
// Dependencies are libcurl and the standard library. Nothing else. Trading
// stacks have opinionated build systems and vendored third-party trees; a
// client that drags in a dependency graph does not get adopted.

#ifndef SPINE_CLIENT_HPP
#define SPINE_CLIENT_HPP

#include <chrono>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace spine {

// Mirrors the decisions the policy engine returns.
enum class Decision {
    Allowed,  // a policy explicitly permitted this
    Blocked,  // a policy denied it, or nothing matched (Spine is default-deny)
    Flagged,  // withheld pending human approval
};

const char* to_string(Decision d);

struct Result {
    bool allowed = false;
    Decision decision = Decision::Blocked;
    std::string reason;
    std::string audit_event_id;

    // True when the verdict was produced locally because the call failed or
    // ran out of budget, rather than by Spine. Worth logging separately: a
    // rising count means the control is degrading, even though orders are
    // still being refused correctly.
    bool failed_closed = false;

    // Round-trip time actually observed, for your own latency histograms.
    std::chrono::microseconds latency{0};
};

namespace detail {
struct Shared;
}  // namespace detail

struct Config {
    std::string base_url;  // e.g. "http://127.0.0.1:8000"
    std::string org_key;   // "spine_..."

    // Hard ceiling on a single intercept call.
    std::chrono::milliseconds timeout{50};

    // Leave false. See the header comment.
    bool fail_open = false;

    // Keep connections open between calls instead of paying a TCP handshake
    // per order. Copies of a Client share one pool. Turn off only if your
    // network drops idle connections in ways libcurl cannot detect.
    bool reuse_connections = true;
};

// One action an agent wants to take. Deliberately flat: everything Spine
// records is a string, and the audit chain hashes what it is given.
struct Action {
    std::string agent_id;
    std::string action_type;      // "order.place", "marketdata.read", ...
    std::string target_resource;  // symbol, feed name, file path
    std::vector<std::pair<std::string, std::string>> metadata;

    // Optional. When set, Spine scores this action against the session's
    // declared plan (its mandate) on the worker. Without it, no model is
    // called and only deterministic policy applies.
    std::string session_id;
};

// A handle to Spine. Copies share one connection pool (and, below, one set of
// stats and one breaker), so copy freely and use from many threads.
class Client {
public:
    explicit Client(Config config);

    // Ask Spine whether this action may proceed. Never throws: a transport
    // failure becomes a Result with failed_closed set, because a gate that
    // throws on the order path will eventually be wrapped in a bare catch
    // that swallows it.
    Result intercept(const Action& action) const;

    // Same call, with a budget just for this request.
    //
    // Use it to warm the path before the open. The first request into a cold
    // stack is far slower than the steady state — measured locally at roughly
    // 55-80 ms against 6-9 ms once warm — so with a 50 ms budget the first
    // order of the session fails closed. That is safe but useless: it stops
    // the one order nobody wanted stopped, and teaches the desk to distrust
    // the gate.
    //
    // Warming has to go through this method rather than a health check,
    // because what is cold is the authentication, policy cache, and audit
    // write path, and /health touches none of them. Give it a budget large
    // enough to actually complete: a request aborted at 50 ms warms nothing,
    // which is a trap worth stating plainly.
    Result intercept_with_budget(const Action& action,
                                 std::chrono::milliseconds budget) const;

    const Config& config() const { return config_; }

private:
    Result perform(const Action& action, std::chrono::milliseconds budget) const;

    Config config_;
    std::shared_ptr<detail::Shared> shared_;
};

// Exposed for testing, and for reuse if you post the body yourself.
namespace detail {
std::string json_escape(const std::string& raw);
std::string build_intercept_body(const Action& action);
Result parse_intercept_response(const std::string& body);
}  // namespace detail

}  // namespace spine

#endif  // SPINE_CLIENT_HPP
