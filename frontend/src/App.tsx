import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Suspense, lazy, useEffect, useState } from "react";
import { useAuth } from "@/auth/useAuth";
import { hasPermission, useAuthStore, type Me } from "@/auth/store";
import { refreshAccessToken } from "@/api/client";
import { useMe } from "@/api/hooks";
import { AppShell } from "@/components/layout/AppShell";
import { LoginPage } from "@/pages/LoginPage";
import { LivePage } from "@/pages/LivePage";
import { DashboardPage } from "@/pages/DashboardPage";
import { Skeleton } from "@/components/ui/skeleton";

/* Live Parking (и панели Dashboard, которые он использует) — ядро операторской работы, грузится сразу;
   остальные экраны — отдельными чанками. */
const SessionsPage = lazy(() => import("@/pages/SessionsPage").then((m) => ({ default: m.SessionsPage })));
const PaymentsPage = lazy(() => import("@/pages/PaymentsPage").then((m) => ({ default: m.PaymentsPage })));
const DevicesPage = lazy(() => import("@/pages/DevicesPage").then((m) => ({ default: m.DevicesPage })));
const IntegrationsPage = lazy(() => import("@/pages/IntegrationsPage").then((m) => ({ default: m.IntegrationsPage })));
const AccessPage = lazy(() => import("@/pages/VehiclesPage").then((m) => ({ default: m.VehiclesPage })));
const UsersPage = lazy(() => import("@/pages/UsersPage").then((m) => ({ default: m.UsersPage })));
const LotsPage = lazy(() => import("@/pages/LotsPage").then((m) => ({ default: m.LotsPage })));
const SignupPage = lazy(() => import("@/pages/SignupPage").then((m) => ({ default: m.SignupPage })));
const WelcomePage = lazy(() => import("@/pages/WelcomePage").then((m) => ({ default: m.WelcomePage })));
const SubscriptionPage = lazy(() => import("@/pages/SubscriptionPage").then((m) => ({ default: m.SubscriptionPage })));
const JoinPage = lazy(() => import("@/pages/JoinPage").then((m) => ({ default: m.JoinPage })));
const ChangePasswordPage = lazy(() => import("@/pages/ChangePasswordPage").then((m) => ({ default: m.ChangePasswordPage })));
const MapPage = lazy(() => import("@/pages/MapPage").then((m) => ({ default: m.MapPage })));
const PlatformPage = lazy(() => import("@/pages/PlatformPage").then((m) => ({ default: m.PlatformPage })));

/** Стартовый экран по правам: операторская — «Живая парковка», владелец платформы — список клиентов. */
function Home() {
  const me = useAuthStore((s) => s.me);
  if (!hasPermission(me, "sessions:read") && hasPermission(me, "platform:view")) return <Navigate to="/platform" replace />;
  return <Navigate to="/live" replace />;
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { authenticated } = useAuth();
  const location = useLocation();
  const setMe = useAuthStore((s) => s.setMe);
  const me = useMe(authenticated);
  useEffect(() => {
    if (me.data) setMe(me.data as Me);
  }, [me.data, setMe]);
  if (!authenticated) return <Navigate to="/login" state={{ from: location }} replace />;
  if (me.isPending) return <Skeleton className="m-4 h-12 w-64" />;
  // временный пароль администратора: пока не сменён, сервер отвечает 403 PASSWORD_CHANGE_REQUIRED везде, кроме /auth
  if ((me.data as Me | undefined)?.must_change_password && location.pathname !== "/change-password") return <Navigate to="/change-password" replace />;
  return <>{children}</>;
}

/** При загрузке приложения пробуем восстановить сессию по refresh-cookie (access живёт только в памяти). */
function Bootstrap({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(useAuthStore.getState().accessToken !== null);
  useEffect(() => {
    if (ready) return;
    void refreshAccessToken().finally(() => setReady(true));
  }, [ready]);
  if (!ready) return <Skeleton className="m-4 h-12 w-64" />;
  return <Suspense fallback={<Skeleton className="m-4 h-12 w-64" />}>{children}</Suspense>;
}

export function App() {
  return (
    <Bootstrap>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/join/:token" element={<JoinPage />} />
        <Route
          path="/change-password"
          element={
            <RequireAuth>
              <ChangePasswordPage />
            </RequireAuth>
          }
        />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route index element={<Home />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="live" element={<LivePage />} />
          <Route path="sessions" element={<SessionsPage />} />
          <Route path="payments" element={<PaymentsPage />} />
          <Route path="lots" element={<LotsPage />} />
          <Route path="devices" element={<DevicesPage />} />
          <Route path="cameras" element={<Navigate to="/devices" replace />} />
          <Route path="barriers" element={<Navigate to="/devices?tab=barriers" replace />} />
          <Route path="map" element={<MapPage />} />
          <Route path="integrations" element={<IntegrationsPage />} />
          <Route path="access" element={<AccessPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="welcome" element={<WelcomePage />} />
          <Route path="subscription" element={<SubscriptionPage />} />
          <Route path="platform" element={<PlatformPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Bootstrap>
  );
}
