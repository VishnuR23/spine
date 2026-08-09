// Tests for the Spine C++ client and the pre-trade gate.
//
// No test framework: a project that vendors GoogleTest to assert twenty
// things has bought a dependency it did not need. These run offline, except
// the fail-closed cases, which point at a port nothing listens on.

#include <chrono>
#include <cstdio>
#include <string>

#include "spine/client.hpp"
#include "spine/finance.hpp"

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool condition, const char* what) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::printf("  FAIL  %s\n", what);
    }
}

void check_eq(const std::string& actual, const std::string& expected,
              const char* what) {
    ++g_checks;
    if (actual != expected) {
        ++g_failures;
        std::printf("  FAIL  %s\n        expected: %s\n        actual:   %s\n",
                    what, expected.c_str(), actual.c_str());
    }
}

// ---------------------------------------------------------------------------

void test_json_escaping() {
    using spine::detail::json_escape;
    check_eq(json_escape("AAPL"), "AAPL", "plain symbol is unchanged");
    check_eq(json_escape("a\"b"), "a\\\"b", "double quote is escaped");
    check_eq(json_escape("a\\b"), "a\\\\b", "backslash is escaped");
    check_eq(json_escape("a\nb"), "a\\nb", "newline is escaped");
    check_eq(json_escape(std::string("a\x01") + "b"), "a\\u0001b",
             "control character is escaped as \\u00xx");
}

void test_body_construction() {
    spine::Action a;
    a.agent_id = "agent-1";
    a.action_type = "order.place";
    a.target_resource = "AAPL";

    const std::string body = spine::detail::build_intercept_body(a);
    check(body.find("\"agent_id\":\"agent-1\"") != std::string::npos,
          "body carries agent_id");
    check(body.find("\"action_type\":\"order.place\"") != std::string::npos,
          "body carries action_type");
    check(body.find("\"target_resource\":\"AAPL\"") != std::string::npos,
          "body carries target_resource");
    check(body.find("session_id") == std::string::npos,
          "session_id is omitted when empty, so no model call is triggered");

    a.session_id = "sess-9";
    a.metadata = {{"venue", "XNAS"}};
    const std::string with_session = spine::detail::build_intercept_body(a);
    check(with_session.find("\"session_id\":\"sess-9\"") != std::string::npos,
          "session_id is included when set");
    check(with_session.find("\"venue\":\"XNAS\"") != std::string::npos,
          "metadata is included");
}

void test_response_parsing() {
    using spine::Decision;

    const std::string allowed =
        R"({"allowed":true,"decision":"allowed","reason":"Allow reads",)"
        R"("audit_event_id":"abc-123"})";
    auto r = spine::detail::parse_intercept_response(allowed);
    check(r.allowed, "allowed:true parses as allowed");
    check(r.decision == Decision::Allowed, "decision parses as Allowed");
    check_eq(r.reason, "Allow reads", "reason is extracted");
    check_eq(r.audit_event_id, "abc-123", "audit_event_id is extracted");
    check(!r.failed_closed, "a real verdict is not marked failed_closed");

    const std::string blocked =
        R"({"allowed":false,"decision":"blocked","reason":"Restricted list"})";
    r = spine::detail::parse_intercept_response(blocked);
    check(!r.allowed, "allowed:false parses as not allowed");
    check(r.decision == Decision::Blocked, "decision parses as Blocked");

    const std::string flagged =
        R"({"allowed":false,"decision":"flagged","reason":"Four-eyes review"})";
    r = spine::detail::parse_intercept_response(flagged);
    check(!r.allowed, "a flagged order is withheld, not sent");
    check(r.decision == Decision::Flagged, "decision parses as Flagged");

    // The important one: anything we cannot read is a refusal.
    r = spine::detail::parse_intercept_response("<html>502 Bad Gateway</html>");
    check(!r.allowed, "an unparseable response does not permit the order");
    check(r.failed_closed, "an unparseable response is marked failed_closed");

    r = spine::detail::parse_intercept_response(R"({"decision":"allowed"})");
    check(!r.allowed, "a response missing `allowed` does not permit the order");
}

void test_notional_and_banding() {
    using namespace spine::finance;
    NotionalBands bands;  // review at 1M, refuse at 10M

    Order small{"AAPL", Side::Buy, 100, 150.0, "XNAS", "stat-arb", "t-1"};
    check(notional(small) == 15000.0, "notional is quantity times limit price");
    check_eq(action_type_for(small, bands), "order.place",
             "an ordinary order maps to order.place");

    Order large{"AAPL", Side::Buy, 20000, 150.0, "XNAS", "stat-arb", "t-1"};
    check(notional(large) == 3000000.0, "3M notional");
    check_eq(action_type_for(large, bands), "order.place.large",
             "above the review threshold maps to order.place.large");

    Order huge{"AAPL", Side::Buy, 100000, 150.0, "XNAS", "stat-arb", "t-1"};
    check_eq(action_type_for(huge, bands), "order.place.block",
             "above the refuse threshold maps to order.place.block");

    // Boundaries are inclusive, so a threshold set at exactly 1M reviews at 1M.
    Order at_review{"AAPL", Side::Buy, 10000, 100.0, "XNAS", "stat-arb", "t-1"};
    check(notional(at_review) == 1000000.0, "exactly 1M");
    check_eq(action_type_for(at_review, bands), "order.place.large",
             "the review threshold is inclusive");

    // The one that matters: an unpriced order must not slip under the bands.
    Order market{"AAPL", Side::Buy, 100000, 0.0, "XNAS", "stat-arb", "t-1"};
    check(notional(market) == 0.0, "an unpriced order has unknown notional");
    check_eq(action_type_for(market, bands), "order.place.unpriced",
             "an unpriced order is routed for review, not treated as zero");
}

void test_fails_closed_when_spine_is_unreachable() {
    spine::Config cfg;
    cfg.base_url = "http://127.0.0.1:1";  // nothing listens here
    cfg.org_key = "spine_not_a_real_key";
    cfg.timeout = std::chrono::milliseconds(250);

    spine::finance::OrderGate gate(spine::Client(cfg), "agent-1");
    const spine::finance::Order o{"AAPL", spine::finance::Side::Buy,
                                  100, 150.0, "XNAS", "stat-arb", "t-1"};
    const auto r = gate.check(o);

    check(!r.allowed, "an unreachable Spine does not permit the order");
    check(r.decision == spine::Decision::Blocked, "the verdict is Blocked");
    check(r.failed_closed, "the refusal is marked as locally produced");
    check(!r.reason.empty(), "a reason is supplied for the operator");
    check(r.latency.count() > 0, "latency is measured even on failure");
}

void test_fail_open_is_available_but_explicit() {
    spine::Config cfg;
    cfg.base_url = "http://127.0.0.1:1";
    cfg.org_key = "spine_not_a_real_key";
    cfg.timeout = std::chrono::milliseconds(250);
    cfg.fail_open = true;  // deliberately opted into

    const spine::Client client(cfg);
    spine::Action a;
    a.agent_id = "agent-1";
    a.action_type = "marketdata.read";
    a.target_resource = "us-equities-l1";

    const auto r = client.intercept(a);
    check(r.allowed, "fail_open permits when Spine is unreachable");
    check(r.failed_closed,
          "the result is still flagged as locally produced, not a real verdict");
}

}  // namespace

int main() {
    std::printf("\nspine C++ client tests\n\n");

    test_json_escaping();
    test_body_construction();
    test_response_parsing();
    test_notional_and_banding();
    test_fails_closed_when_spine_is_unreachable();
    test_fail_open_is_available_but_explicit();

    std::printf("\n  %d checks, %d failures\n\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
