#include "spine/finance.hpp"

#include <cstdio>
#include <utility>
#include <vector>

namespace spine {
namespace finance {

const char* to_string(Side s) {
    return s == Side::Buy ? "BUY" : "SELL";
}

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
    // Unknown notional must not silently mean "small": treating it as zero
    // would let a market order become the cheapest way past a control.
    if (pricing_of(o) == Pricing::Unpriced) return prefix + ".unpriced";
    const double value = notional(o, bands);
    if (value >= bands.refuse_at) return prefix + ".block";
    if (value >= bands.review_at) return prefix + ".large";
    return prefix;
}

std::string action_type_for(const Order& o, const NotionalBands& bands) {
    return action_type_for("order.place", o, bands);
}

namespace {

std::string format_number(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.2f", v);
    return std::string(buf);
}

using Metadata = std::vector<std::pair<std::string, std::string>>;

// Everything here lands verbatim in the hash-chained audit log, which is the
// point: this is the record of what was asked and on whose behalf.
//
// For the same reason, keep it to trade facts. No client identifiers, no
// research or rationale text, nothing that would turn the audit trail into a
// store of material non-public information or personal data. Spine redacts
// common secret shapes on the way through, but that is a safety net, not a
// licence to send them.
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
    action.metadata = order_metadata(order, bands_);
    return client_.intercept(action);
}

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
