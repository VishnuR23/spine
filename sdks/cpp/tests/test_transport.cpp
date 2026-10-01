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
