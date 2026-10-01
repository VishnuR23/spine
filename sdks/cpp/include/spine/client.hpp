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

#include <array>
#include <chrono>
#include <cstdint>
#include <functional>
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

enum class BreakerState {
    Closed,    // normal: every call goes to Spine
    Open,      // Spine keeps failing: answer locally, no network call
    HalfOpen,  // cooldown over: one probe is in flight
};

const char* to_string(BreakerState s);

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

    // Consecutive locally-produced verdicts that open the circuit breaker.
    // While open, calls are answered locally at once instead of each one
    // waiting out the timeout. 0 disables the breaker.
    int breaker_threshold = 5;

    // How long the breaker stays open before one probe is let through.
    std::chrono::milliseconds breaker_cooldown{5000};

    // Optional. Called on the calling thread after each breaker transition;
    // wire it to your alerting. Exceptions it throws are swallowed.
    std::function<void(BreakerState)> on_breaker_change;
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

    // For actions that reduce risk, such as cancelling an order. Asks Spine
    // as usual and honours a real verdict, including a deliberate block. But
    // when the verdict would be produced locally -- Spine unreachable, budget
    // exceeded, breaker open, halted -- it allows, because refusing a cancel
    // keeps the risk on. Result::failed_closed still marks it as local.
    Result intercept_risk_reducing(const Action& action) const;

    // Local kill switch. While halted, every call except risk-reducing ones
    // is refused at once, with no network call, regardless of fail_open.
    // Copies of this Client share the halt.
    void halt(const std::string& reason) const;
    void resume() const;
    bool halted() const;

    Stats stats() const;
    BreakerState breaker_state() const;

private:
    Result intercept_impl(const Action& action, std::chrono::milliseconds budget,
                          bool risk_reducing) const;
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
