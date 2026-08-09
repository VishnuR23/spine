// Pre-trade gate built on the Spine client.
//
// The shape of this file is dictated by one fact about Spine's policy engine:
// it matches strings. Action type, a regex over the target resource, and a
// UTC time window. It cannot compare numbers, so it cannot express "block
// orders above ten million dollars".
//
// The resolution is to move the arithmetic to the caller and the authority to
// the server. The gate computes notional locally and selects an action type
// from a band — order.place, order.place.large, order.place.block — and the
// policy that governs each band lives in Spine, where compliance can change
// it without a rebuild of the trading system. The client decides which
// question to ask; it never decides the answer.
//
// The same split governs restricted lists. The gate sends the symbol as the
// target resource and holds no list of its own, because a restricted list
// changes intraday and a copy compiled into a binary is a copy that is wrong.
// Spine matches it with a regex and denies centrally.
//
// What this gate does NOT do: position or exposure limits, which need state
// this client does not have; anything requiring a view of the book. Those
// belong in a risk system. Spine governs whether the agent was permitted to
// act, and leaves an audit trail proving what was asked and answered.

#ifndef SPINE_FINANCE_HPP
#define SPINE_FINANCE_HPP

#include <string>

#include "spine/client.hpp"

namespace spine {
namespace finance {

enum class Side { Buy, Sell };

const char* to_string(Side s);

struct Order {
    std::string symbol;       // becomes target_resource; policy matches on it
    Side side = Side::Buy;
    long long quantity = 0;
    double limit_price = 0.0;  // 0 for a market order
    std::string venue;
    std::string strategy_id;
    std::string trader_id;    // the human accountable for the mandate
};

// quantity × limit_price. Market orders have no limit price, so their
// notional is unknown here and reported as 0 — see action_type_for for how
// that is handled, because "unknown" must not silently mean "small".
double notional(const Order& o);

// Thresholds, in the account's currency. Defaults are illustrative; set them
// from your own risk appetite.
struct NotionalBands {
    // At or above this, ask for a human. Maps to order.place.large, which the
    // shipped policy pack flags — Spine opens an approval ticket and issues a
    // time-boxed grant when someone signs off. This is four-eyes review.
    double review_at = 1'000'000.0;

    // At or above this, do not ask — refuse. Maps to order.place.block.
    double refuse_at = 10'000'000.0;
};

// Selects the action type for an order. Market orders (no limit price) are
// treated as unpriced and routed to order.place.unpriced rather than assumed
// cheap: an unbounded order is exactly the one you want a human to see.
std::string action_type_for(const Order& o, const NotionalBands& bands);

// Wraps a Client with order-shaped calls.
class OrderGate {
public:
    OrderGate(Client client, std::string agent_id, NotionalBands bands = {});

    // Ask whether this order may be sent. Returns Spine's verdict, or a
    // fail-closed refusal if Spine could not answer within the budget.
    //
    // session_id is optional but strongly recommended: with it, the order is
    // also scored against the mandate the agent declared when the session
    // opened, which is what catches an agent drifting away from its stated
    // strategy while every individual order remains permitted.
    Result check(const Order& order, const std::string& session_id = "") const;

    // Market data carries licensing obligations that differ per feed, and
    // entitlement is exactly the kind of rule that belongs in central policy
    // rather than in each consumer.
    Result check_market_data(const std::string& feed,
                             const std::string& session_id = "") const;

    // Start-of-day self test. Runs one real intercept through the full
    // enforcement path — auth, policy cache, audit write — with a budget
    // large enough to complete, so that the first live order meets a warm
    // stack instead of paying the cold cost and being refused.
    //
    // It deliberately uses the real path rather than a health check, and it
    // deliberately leaves a row in the audit log. Desks already send a test
    // ticket through risk before the open; the record that the control was
    // exercised is a feature, not noise.
    Result pre_open_check(
        const std::string& feed = "us-equities-ref",
        std::chrono::milliseconds budget = std::chrono::milliseconds(2000)) const;

    const NotionalBands& bands() const { return bands_; }

private:
    Client client_;
    std::string agent_id_;
    NotionalBands bands_;
};

}  // namespace finance
}  // namespace spine

#endif  // SPINE_FINANCE_HPP
