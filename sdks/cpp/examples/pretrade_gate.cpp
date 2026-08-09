// A pre-trade gate, end to end.
//
// Sends four orders through Spine and prints each verdict. Run it against a
// Spine seeded with the finance policy pack:
//
//   make demo
//   python examples/finance/seed_finance_policies.py     # prints the exports
//   export SPINE_ORG_KEY=spine_...  SPINE_AGENT_ID=...
//   ./build/pretrade_gate
//
// Without a running Spine it still demonstrates the property that matters
// most on an order path: every order is refused, because the gate fails
// closed.

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "spine/client.hpp"
#include "spine/finance.hpp"

namespace {

const char* env_or(const char* name, const char* fallback) {
    const char* v = std::getenv(name);
    return (v != nullptr && *v != '\0') ? v : fallback;
}

void print_verdict(const spine::finance::Order& order,
                   const std::string& action_type,
                   const spine::Result& r) {
    const char* mark = r.allowed ? "SENT   " : "STOPPED";
    std::printf("  %s  %-4s %-5s %8lld @ %-9.2f  %-22s  %s\n",
                mark,
                spine::finance::to_string(order.side),
                order.symbol.c_str(),
                order.quantity,
                order.limit_price,
                action_type.c_str(),
                spine::to_string(r.decision));
    std::printf("           reason: %s\n",
                r.reason.empty() ? "(none given)" : r.reason.c_str());
    std::printf("           latency: %lld us%s\n\n",
                static_cast<long long>(r.latency.count()),
                r.failed_closed ? "   [failed closed — verdict produced locally]" : "");
}

}  // namespace

int main() {
    spine::Config cfg;
    cfg.base_url = env_or("SPINE_BASE_URL", "http://127.0.0.1:8000");
    cfg.org_key = env_or("SPINE_ORG_KEY", "spine_missing_key");
    // 50 ms is the budget the intercept path is designed around. If the gate
    // cannot answer within it, the order does not go. Override to see how
    // much headroom your own deployment actually has.
    cfg.timeout = std::chrono::milliseconds(std::atoi(env_or("SPINE_TIMEOUT_MS", "50")));
    cfg.fail_open = false;

    const std::string agent_id = env_or("SPINE_AGENT_ID", "00000000-0000-0000-0000-000000000000");
    const std::string session_id = env_or("SPINE_SESSION_ID", "");

    spine::finance::NotionalBands bands;  // review at 1M, refuse at 10M
    spine::finance::OrderGate gate(spine::Client(cfg), agent_id, bands);

    using spine::finance::Order;
    using spine::finance::Side;

    const std::vector<std::pair<const char*, Order>> cases = {
        {"ordinary order, within every limit",
         Order{"AAPL", Side::Buy, 100, 150.00, "XNAS", "stat-arb", "t-1"}},

        {"restricted symbol — the list lives in Spine, not in this binary",
         Order{"RSTR", Side::Buy, 100, 42.00, "XNAS", "stat-arb", "t-1"}},

        {"large notional — four-eyes review before it can go",
         Order{"MSFT", Side::Buy, 20000, 300.00, "XNAS", "stat-arb", "t-1"}},

        {"market order — unpriced, so notional is unbounded",
         Order{"NVDA", Side::Sell, 5000, 0.00, "XNAS", "stat-arb", "t-1"}},
    };

    std::printf("\n  Spine pre-trade gate  ->  %s\n", cfg.base_url.c_str());
    std::printf("  budget %lld ms, fail-closed\n",
                static_cast<long long>(cfg.timeout.count()));

    // Start of day: run one real request through the enforcement path so the
    // first live order meets a warm stack rather than paying the cold cost.
    const spine::Result opening = gate.pre_open_check();
    std::printf("  pre-open check: %s (%lld us)%s\n\n",
                opening.failed_closed ? "Spine did NOT answer" : spine::to_string(opening.decision),
                static_cast<long long>(opening.latency.count()),
                opening.failed_closed ? "  — orders will be refused" : "");

    int stopped = 0;
    int reached_spine = 0;
    for (const auto& c : cases) {
        std::printf("  %s\n", c.first);
        const std::string action_type =
            spine::finance::action_type_for(c.second, bands);
        const spine::Result r = gate.check(c.second, session_id);
        print_verdict(c.second, action_type, r);
        if (!r.allowed) ++stopped;
        if (!r.failed_closed) ++reached_spine;
    }

    std::printf("  %d of %zu orders stopped.\n\n", stopped, cases.size());

    if (reached_spine > 0) {
        std::printf("  Those %d decisions are now rows in the hash-chained audit\n"
                    "  log, verifiable with GET /v1/audit/verify.\n\n",
                    reached_spine);
    } else {
        std::printf("  Spine was never reached, so nothing was recorded — these\n"
                    "  refusals were produced locally. That is the intended\n"
                    "  behaviour: an unreachable control stops trading rather\n"
                    "  than waving it through. Start Spine and run again to see\n"
                    "  policy actually applied.\n\n");
    }
    return 0;
}
