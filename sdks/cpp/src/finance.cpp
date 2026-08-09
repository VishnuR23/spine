#include "spine/finance.hpp"

#include <cstdio>
#include <utility>

namespace spine {
namespace finance {

const char* to_string(Side s) {
    return s == Side::Buy ? "BUY" : "SELL";
}

double notional(const Order& o) {
    if (o.limit_price <= 0.0) return 0.0;  // unpriced; see action_type_for
    return static_cast<double>(o.quantity) * o.limit_price;
}

std::string action_type_for(const Order& o, const NotionalBands& bands) {
    if (o.limit_price <= 0.0) {
        // An order with no limit price has unbounded notional. Treating it as
        // zero would let it slip under every band — the failure mode where a
        // market order becomes the cheapest way past a control.
        return "order.place.unpriced";
    }
    const double value = notional(o);
    if (value >= bands.refuse_at) return "order.place.block";
    if (value >= bands.review_at) return "order.place.large";
    return "order.place";
}

namespace {

std::string format_number(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.2f", v);
    return std::string(buf);
}

}  // namespace

OrderGate::OrderGate(Client client, std::string agent_id, NotionalBands bands)
    : client_(std::move(client)),
      agent_id_(std::move(agent_id)),
      bands_(bands) {}

Result OrderGate::check(const Order& order, const std::string& session_id) const {
    Action action;
    action.agent_id = agent_id_;
    action.action_type = action_type_for(order, bands_);
    action.target_resource = order.symbol;
    action.session_id = session_id;

    // Everything here lands verbatim in the hash-chained audit log, which is
    // the point: this is the record of what was asked and on whose behalf.
    //
    // For the same reason, keep it to trade facts. No client identifiers, no
    // research or rationale text, nothing that would turn the audit trail
    // into a store of material non-public information or personal data.
    // Spine redacts common secret shapes on the way through, but that is a
    // safety net, not a licence to send them.
    action.metadata = {
        {"side", to_string(order.side)},
        {"quantity", std::to_string(order.quantity)},
        {"limit_price", format_number(order.limit_price)},
        {"notional", format_number(notional(order))},
        {"venue", order.venue},
        {"strategy_id", order.strategy_id},
        {"trader_id", order.trader_id},
    };

    return client_.intercept(action);
}

Result OrderGate::check_market_data(const std::string& feed,
                                    const std::string& session_id) const {
    Action action;
    action.agent_id = agent_id_;
    action.action_type = "marketdata.read";
    action.target_resource = feed;
    action.session_id = session_id;
    return client_.intercept(action);
}

Result OrderGate::pre_open_check(const std::string& feed,
                                 std::chrono::milliseconds budget) const {
    Action action;
    action.agent_id = agent_id_;
    action.action_type = "marketdata.read";
    action.target_resource = feed;
    // No session: this is an operational check, not an act under the mandate,
    // and it should not consume a plan evaluation or move the drift score.
    return client_.intercept_with_budget(action, budget);
}

}  // namespace finance
}  // namespace spine
