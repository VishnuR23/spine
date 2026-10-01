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
