// Transport behaviour against a real local HTTP server: connection reuse,
// stats, circuit breaker, manual halt.

#include <chrono>
#include <cstdlib>
#include <new>
#include <cstdint>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "check.hpp"
#include "fake_spine.hpp"
#include "spine/client.hpp"

// Fault injection: when t_fail_alloc is set, the next allocation on this
// thread throws std::bad_alloc. Thread-local, so the fake server's own
// threads are never affected.
namespace {
thread_local bool t_fail_alloc = false;
}  // namespace

void* operator new(std::size_t n) {
    if (t_fail_alloc) {
        t_fail_alloc = false;
        throw std::bad_alloc();
    }
    if (void* p = std::malloc(n == 0 ? 1 : n)) return p;
    throw std::bad_alloc();
}
void operator delete(void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }

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

void test_moved_from_client_still_works() {
    FakeSpine server;
    spine::Client a(config_for(server));
    spine::Client b(std::move(a));  // e.g. handed to an OrderGate
    b.intercept(read_action());
    // A moved-from Client must not crash on the order path; it keeps
    // sharing the same pool, stats, breaker, and halt.
    const auto r = a.intercept(read_action());  // NOLINT(bugprone-use-after-move)
    check(r.allowed && !r.failed_closed, "a moved-from Client still gets real verdicts");
    a.halt("x");
    check(b.halted(), "and shares its halt with the Client it was moved into");
}

void test_zero_budget_is_not_unlimited() {
    FakeSpine server;
    server.set_delay(std::chrono::milliseconds(400));
    const spine::Client client(config_for(server));
    const auto started = std::chrono::steady_clock::now();
    const auto r = client.intercept_with_budget(read_action(), std::chrono::milliseconds(0));
    const auto took = std::chrono::steady_clock::now() - started;
    // libcurl reads a 0 ms timeout as "no timeout". A zero budget must
    // still bound the call, not leave the order path waiting on the server.
    check(r.failed_closed && !r.allowed, "a zero budget fails closed");
    check(took < std::chrono::milliseconds(200), "and does not wait for a slow server");
}

void test_exception_during_probe_does_not_wedge_the_breaker() {
    FakeSpine server;
    server.respond(500, "{}");
    spine::Config cfg = config_for(server);
    cfg.breaker_threshold = 1;
    cfg.breaker_cooldown = std::chrono::milliseconds(50);
    const spine::Client client(cfg);
    const spine::Action action = read_action();

    client.intercept(action);  // trips the breaker
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    server.respond(200, spine_test::kAllowed);

    bool threw = false;
    spine::Result r;
    t_fail_alloc = true;  // the probe's first allocation throws
    try {
        r = client.intercept(action);
    } catch (...) {
        threw = true;
    }
    t_fail_alloc = false;
    check(!threw, "an exception inside a check does not escape the order path");
    check(!r.allowed && r.failed_closed, "the check fails closed instead");
    check(client.breaker_state() == spine::BreakerState::Open,
          "the probe slot is released: the breaker re-opens, not stuck half-open");

    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    const auto next = client.intercept(action);
    check(next.allowed && !next.failed_closed, "the next probe reaches a healthy Spine");
    check(client.breaker_state() == spine::BreakerState::Closed, "and closes the breaker");
}

void test_exception_on_a_cancel_still_allows_it() {
    FakeSpine server;
    const spine::Client client(config_for(server));
    const spine::Action action = read_action();
    bool threw = false;
    spine::Result r;
    t_fail_alloc = true;
    try {
        r = client.intercept_risk_reducing(action);
    } catch (...) {
        threw = true;
    }
    t_fail_alloc = false;
    check(!threw && r.allowed && r.failed_closed,
          "an internal error on a risk-reducing call allows it locally");
}

}  // namespace

void run_transport_tests() {
    test_real_verdict_over_http();
    test_connections_are_reused();
    test_reuse_can_be_turned_off();
    test_copies_share_the_pool();
    test_server_closing_connections_is_harmless();
    test_timeout_then_recovery();
    test_stats_count_decisions_and_latency();
    test_stats_are_exact_under_concurrency();
    test_breaker_trips_short_circuits_and_recovers();
    test_failed_probe_reopens();
    test_success_resets_the_failure_count();
    test_open_breaker_honours_fail_open();
    test_threshold_zero_disables_the_breaker();
    test_throwing_callback_is_contained();
    test_halt_blocks_without_a_network_call();
    test_risk_reducing_calls_pass_a_halt_and_an_open_breaker();
    test_real_block_of_a_risk_reducing_call_is_respected();
    test_moved_from_client_still_works();
    test_zero_budget_is_not_unlimited();
    test_exception_during_probe_does_not_wedge_the_breaker();
    test_exception_on_a_cancel_still_allows_it();
}
