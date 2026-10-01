// Transport behaviour against a real local HTTP server: connection reuse,
// stats, circuit breaker, manual halt.

#include <chrono>
#include <cstdint>
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
}
