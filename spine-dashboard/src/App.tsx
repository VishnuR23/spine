import { Navigate, Outlet, Route, Routes, useNavigate } from "react-router";
import { AppShell } from "@/components/AppShell";
import { ApprovalPrompt } from "@/components/ApprovalPrompt";
import { LiveActivityProvider } from "@/contexts/LiveActivityContext";
import { Spinner } from "@/components/ui";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { AgentsPage } from "@/pages/AgentsPage";
import { ApprovalsPage } from "@/pages/ApprovalsPage";
import { AuditPage } from "@/pages/AuditPage";
import { GuidePage } from "@/pages/GuidePage";
import { HomePage } from "@/pages/HomePage";
import { InterceptPage } from "@/pages/InterceptPage";
import { LandingPage } from "@/pages/LandingPage";
import { LoginPage } from "@/pages/LoginPage";
import { OrgsPage } from "@/pages/OrgsPage";
import { PoliciesPage } from "@/pages/PoliciesPage";
import { SessionsPage } from "@/pages/SessionsPage";
import { SessionDetailPage } from "@/pages/SessionDetailPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { WebhooksPage } from "@/pages/WebhooksPage";

function LoadingScreen() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-spine-bg">
      <Spinner className="h-6 w-6 border-spine-border border-t-white" />
      <p className="text-sm text-spine-muted">Loading…</p>
    </div>
  );
}

function ProtectedLayout() {
  const { ready, user, logout } = useAuth();
  const navigate = useNavigate();

  if (!ready) return <LoadingScreen />;
  if (!user) return <Navigate to="/welcome" replace />;

  const handleLogout = async () => {
    await logout();
    navigate("/welcome");
  };

  return (
    <LiveActivityProvider>
      <AppShell onLogout={handleLogout}>
        <Outlet />
        <ApprovalPrompt />
      </AppShell>
    </LiveActivityProvider>
  );
}

function RootRedirect() {
  const { ready, user } = useAuth();
  if (!ready) return <LoadingScreen />;
  return user ? <Navigate to="/dashboard" replace /> : <Navigate to="/welcome" replace />;
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/" element={<RootRedirect />} />
        <Route path="/welcome" element={<LandingPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedLayout />}>
          <Route path="/dashboard" element={<HomePage />} />
          <Route path="/guide" element={<GuidePage />} />
          <Route path="/intercept" element={<InterceptPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/policies" element={<PoliciesPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/sessions/:sessionId" element={<SessionDetailPage />} />
          <Route path="/webhooks" element={<WebhooksPage />} />
          <Route path="/orgs" element={<OrgsPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthProvider>
  );
}
