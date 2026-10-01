// Order lifecycle: pricing, amends, cancels.

#include <chrono>
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

}  // namespace

void run_lifecycle_tests() {
    test_market_order_priced_off_reference();
    test_collar_can_push_an_order_over_a_band();
    test_limit_price_wins_over_reference();
    test_unpriced_without_reference_is_unchanged();
    test_prefix_overload();
    test_check_sends_full_order_metadata();
    test_amend_bands_on_the_new_size();
    test_amend_fails_closed_like_a_new_order();
}
