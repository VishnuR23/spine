import { Link } from "react-router";
import { useLiveActivity } from "@/contexts/LiveActivityContext";
import { Button } from "@/components/ui";

export function ApprovalPrompt() {
  const { pendingApprovals, decideApproval, dismissApproval } = useLiveActivity();
  const top = pendingApprovals[0];
  if (!top) return null;

  async function onAllow() {
    try {
      await decideApproval(top.id, "approve");
    } catch {
      /* Approvals page can retry */
    }
  }

  async function onBlock() {
    try {
      await decideApproval(top.id, "reject");
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="fixed bottom-6 right-6 z-50 w-full max-w-md">
      <div className="surface-panel overflow-hidden rounded-xl border border-spine-border-strong shadow-2xl">
        <div className="border-b border-spine-border bg-spine-elevated/80 px-4 py-3">
          <p className="text-sm font-medium text-spine-fg">Approval required</p>
          <p className="mt-0.5 text-xs text-spine-muted">
            An agent action was flagged. Allow grants a one-time retry for this exact action.
          </p>
        </div>
        <div className="space-y-2 px-4 py-3 text-sm">
          <p>
            <span className="text-spine-muted">Action:</span>{" "}
            <span className="font-mono text-xs text-spine-fg">{top.action_type}</span>
          </p>
          {top.target_resource ? (
            <p className="truncate">
              <span className="text-spine-muted">Target:</span>{" "}
              <span className="font-mono text-xs">{top.target_resource}</span>
            </p>
          ) : null}
          {pendingApprovals.length > 1 ? (
            <p className="text-2xs text-spine-subtle">+{pendingApprovals.length - 1} more in queue</p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-spine-border bg-spine-bg/50 px-4 py-3">
          <Button size="sm" onClick={() => void onAllow()}>
            Allow
          </Button>
          <Button size="sm" variant="secondary" onClick={() => void onBlock()}>
            Block
          </Button>
          <Link
            to="/approvals"
            className="ml-auto text-xs text-spine-accent hover:text-white transition-colors"
          >
            Open queue →
          </Link>
          <button
            type="button"
            onClick={() => dismissApproval(top.id)}
            className="text-2xs text-spine-subtle hover:text-spine-muted"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
