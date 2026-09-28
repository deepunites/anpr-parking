import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { fmt, useTranslation } from "@/i18n";
import { Activity, BadgeDollarSign, Building2, Cpu, CreditCard, KeyRound, LayoutDashboard, ListOrdered, LogOut, Map, Menu, ParkingSquare, Plug, Rocket, ShieldCheck, Users, Wifi, WifiOff, X } from "lucide-react";
import { useAuth } from "@/auth/useAuth";
import { hasPermission } from "@/auth/store";
import { useCurrentShift, useLots, useReviewQueue } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { setLang } from "@/i18n";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/input";
import { ToastViewport } from "@/components/ui/toast";
import { useThemeStore, type ThemeMode } from "@/theme/store";
import type { SocketStatus } from "@/realtime/socket";

/* Навигация группами; пункт, недоступный роли, не показывается (не «серый»). Ключи — i18n, права — из /auth/me. */
interface NavItem { to: string; key: string; permission: string; icon: ReactNode; badge?: "review" }
const GROUPS: { key: string; items: NavItem[] }[] = [
  {
    key: "nav.groupWork",
    items: [
      { to: "/live", key: "nav.live", permission: "sessions:read", icon: <Activity className="h-[18px] w-[18px]" aria-hidden="true" />, badge: "review" },
      { to: "/sessions", key: "nav.sessions", permission: "sessions:read", icon: <ListOrdered className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/payments", key: "nav.payments", permission: "payment:read", icon: <BadgeDollarSign className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/dashboard", key: "nav.dashboard", permission: "dashboard:view", icon: <LayoutDashboard className="h-[18px] w-[18px]" aria-hidden="true" /> },
    ],
  },
  {
    key: "nav.groupSetup",
    items: [
      { to: "/lots", key: "nav.lots", permission: "lots:read", icon: <ParkingSquare className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/devices", key: "nav.devices", permission: "cameras:read", icon: <Cpu className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/map", key: "nav.map", permission: "lots:read", icon: <Map className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/users", key: "nav.users", permission: "users:read", icon: <Users className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/access", key: "nav.access", permission: "access:read", icon: <ShieldCheck className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/integrations", key: "nav.integrations", permission: "integrations:manage", icon: <Plug className="h-[18px] w-[18px]" aria-hidden="true" /> },
    ],
  },
  {
    key: "nav.groupAccount",
    items: [
      { to: "/welcome", key: "nav.welcome", permission: "lots:write", icon: <Rocket className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/subscription", key: "nav.subscription", permission: "subscription:manage", icon: <CreditCard className="h-[18px] w-[18px]" aria-hidden="true" /> },
      { to: "/platform", key: "nav.platform", permission: "platform:view", icon: <Building2 className="h-[18px] w-[18px]" aria-hidden="true" /> },
    ],
  },
];

/** Баннер подписки: пробный период (сколько дней) или блокировка настроек. Проезд машин не блокируется никогда (A-82). */
function SubscriptionBanner() {
  const { t } = useTranslation();
  const { me } = useAuth();
  if (!me?.tenant_status) return null;
  const canPay = hasPermission(me, "subscription:manage");
  if (me.blocked_reason) {
    return (
      <div role="alert" className="mx-4 mb-2 flex flex-wrap items-center gap-3 rounded-xl border border-bad/50 bg-bad-soft px-3 py-2 text-[13px] font-bold text-bad-ink lg:mx-6" data-testid="sub-banner">
        {me.blocked_reason === "SUSPENDED" ? t("sub.bannerSuspended") : t("sub.bannerExpired")}
        {canPay && (
          <Link to="/subscription" className="ml-auto underline">
            {t("sub.choosePay")}
          </Link>
        )}
      </div>
    );
  }
  if (me.tenant_status !== "TRIAL" || me.trial_days_left == null) return null;
  return (
    <div className={cn("mx-4 mb-2 flex flex-wrap items-center gap-3 rounded-xl px-3 py-2 text-[13px] font-bold lg:mx-6", me.trial_days_left <= 3 ? "bg-warn-soft text-warn-ink" : "bg-accent-soft text-accent-ink")} data-testid="sub-banner">
      {fmt(t("sub.bannerTrial"), { n: me.trial_days_left })}
      {canPay && (
        <Link to="/subscription" className="ml-auto underline">
          {t("sub.choosePay")}
        </Link>
      )}
    </div>
  );
}

export function ConnectionIndicator({ status, stale }: { status: SocketStatus; stale: boolean }) {
  const { t } = useTranslation();
  const ok = status === "open" && !stale;
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="connection-indicator"
      data-status={status}
      data-stale={stale ? "1" : "0"}
      className={cn("flex items-center gap-2 text-[13px] font-bold", ok ? "text-ok-ink" : "text-warn-ink")}
    >
      {ok ? <Wifi className="h-4 w-4" aria-hidden="true" /> : <WifiOff className="h-4 w-4" aria-hidden="true" />}
      <span>{t(`conn.${status}`)}</span>
      {stale && <span className="opacity-80">· {t("conn.stale")}</span>}
    </div>
  );
}

function ThemeToggle() {
  const { t } = useTranslation();
  const { theme, setTheme, scale, setScale } = useThemeStore();
  return (
    <div className="flex items-center gap-2">
      <Segmented<ThemeMode>
        ariaLabel={t("theme.label")}
        size="sm"
        value={theme}
        onChange={setTheme}
        options={[
          { value: "light", label: t("theme.light") },
          { value: "dark", label: t("theme.dark") },
          { value: "auto", label: t("theme.auto") },
        ]}
      />
      <button
        type="button"
        onClick={() => setScale(scale === "md" ? "lg" : "md")}
        className={cn("h-8 rounded-lg px-2 text-[12px] font-bold", scale === "lg" ? "bg-accent-soft text-accent-ink" : "text-muted hover:text-ink")}
        aria-pressed={scale === "lg"}
        title={t("theme.scale")}
        data-testid="scale-toggle"
      >
        A<span className="text-[15px]">A</span>
      </button>
    </div>
  );
}

function ShiftCard() {
  const { t } = useTranslation();
  const { me } = useAuth();
  const enabled = hasPermission(me, "shift:manage");
  const shift = useCurrentShift(enabled);
  if (!enabled || !shift.data) return null;
  const s = shift.data as { opened_at?: string; expected_cash?: { formatted?: string } | null };
  return (
    <div className="mt-auto rounded-xl border border-line bg-surface-2 p-3" data-testid="shift-card">
      <div className="text-[13px] font-extrabold">{t("shift.card")}</div>
      <div className="text-[12px] text-muted">
        {t("shift.since")} {s.opened_at ? new Date(s.opened_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}
        {s.expected_cash?.formatted && <> · {s.expected_cash.formatted}</>}
      </div>
    </div>
  );
}

export function AppShell() {
  const { t, i18n } = useTranslation();
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const lots = useLots(hasPermission(me, "lots:read"));
  const { lotId, setLotId } = useLotStore();
  const review = useReviewQueue(hasPermission(me, "sessions:read"));
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    const items = lots.data ?? [];
    if (items.length > 0 && (lotId === null || !items.some((l) => l.id === lotId))) setLotId(items[0]!.id);
  }, [lots.data, lotId, setLotId]);

  const reviewCount = (review.data ?? []).filter((s) => lotId === null || s.lot_id === lotId).length;
  const initials = (me?.display_name ?? me?.email ?? "?")
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");

  const nav = (
    <nav aria-label="main" className="flex flex-col gap-0.5">
      {GROUPS.map((g) => {
        // «Быстрый старт» нужен новому клиенту — пока идёт пробный период
        const items = g.items.filter((n) => hasPermission(me, n.permission) && (n.to !== "/welcome" || me?.tenant_status === "TRIAL"));
        if (items.length === 0) return null;
        return (
          <div key={g.key} className="mb-2">
            <div className="px-2.5 pt-3 pb-1.5 text-[10px] font-bold uppercase tracking-[0.14em] text-muted">{t(g.key)}</div>
            {items.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                onClick={() => setMenuOpen(false)}
                className={({ isActive }) =>
                  cn("flex items-center gap-2.5 rounded-[10px] px-2.5 py-2.5 text-[14px] font-semibold transition-colors", isActive ? "bg-accent-soft text-accent-ink" : "text-ink-2 hover:bg-surface-2 hover:text-ink")
                }
              >
                {n.icon}
                {t(n.key)}
                {n.badge === "review" && reviewCount > 0 && (
                  <span className="ml-auto rounded-md bg-warn px-1.5 py-0.5 font-mono text-[11px] font-bold text-[#1b1400]" data-testid="nav-review-count">
                    {reviewCount}
                  </span>
                )}
              </NavLink>
            ))}
          </div>
        );
      })}
    </nav>
  );

  const lotSelect = (
    <div className="flex items-center gap-2 rounded-xl border border-line bg-surface px-3 py-1.5 shadow-card">
      <label htmlFor="lot-select" className="text-[11px] font-bold uppercase tracking-[0.06em] text-muted">
        {t("app.lot")}
      </label>
      <select
        id="lot-select"
        className="h-8 max-w-[220px] bg-transparent pr-1 text-[15px] font-extrabold text-ink focus:outline-none"
        value={lotId ?? ""}
        onChange={(e) => setLotId(e.target.value)}
        data-testid="lot-select"
      >
        {(lots.data ?? []).map((lot) => (
          <option key={lot.id} value={lot.id}>
            {lot.name}
          </option>
        ))}
      </select>
    </div>
  );

  return (
    <div className="flex min-h-full">
      {/* Боковая панель: на широком экране — всегда, на планшете — выезжает по кнопке */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-30 flex w-[232px] flex-col border-r border-line bg-surface px-3.5 py-4 transition-transform lg:static lg:translate-x-0",
          menuOpen ? "translate-x-0 shadow-pop" : "-translate-x-full",
        )}
        data-testid="sidebar"
      >
        <div className="mb-2 flex items-center gap-2.5 px-2 py-1.5">
          <span className="grid h-[34px] w-[34px] place-items-center rounded-[10px] bg-[linear-gradient(145deg,#2b6cf6,#7fa7f6)] font-extrabold text-white">P</span>
          <span className="truncate text-[15px] font-extrabold" data-testid="tenant-name">
            {me?.tenant_name ?? t("app.title")}
          </span>
          <Button variant="ghost" size="icon" className="ml-auto lg:hidden" onClick={() => setMenuOpen(false)} aria-label={t("common.close")}>
            <X className="h-5 w-5" aria-hidden="true" />
          </Button>
        </div>
        {nav}
        <ShiftCard />
      </aside>
      {menuOpen && <div className="fixed inset-0 z-20 bg-ink/40 lg:hidden" onClick={() => setMenuOpen(false)} role="presentation" />}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-3 px-4 py-3 lg:px-6">
          <Button variant="outline" size="icon" className="lg:hidden" onClick={() => setMenuOpen(true)} aria-label="Меню" data-testid="menu-button">
            <Menu className="h-5 w-5" aria-hidden="true" />
          </Button>
          {hasPermission(me, "lots:read") && lotSelect}
          <div className="hidden sm:block" id="connection-slot" />
          <div className="ml-auto flex flex-wrap items-center gap-3">
            <ThemeToggle />
            <select
              id="lang-select"
              aria-label={t("app.language")}
              className="h-8 rounded-lg border border-line bg-surface px-2 text-[12.5px] font-bold"
              value={i18n.language}
              onChange={(e) => setLang(e.target.value as "ru" | "uz")}
              data-testid="lang-select"
            >
              <option value="ru">Русский</option>
              <option value="uz">O‘zbekcha</option>
            </select>
            <div className="flex items-center gap-2.5">
              <span className="grid h-[34px] w-[34px] place-items-center rounded-full bg-[linear-gradient(145deg,#f5b301,#f97316)] text-[12px] font-extrabold text-[#1b1400]" aria-hidden="true">
                {initials}
              </span>
              <div className="hidden leading-tight md:block">
                <div className="text-[13px] font-bold">{me?.display_name ?? me?.email}</div>
                <div className="text-[11.5px] font-semibold text-muted" data-testid="me-role">
                  {me?.role ? t(`roles.${me.role}`) : ""}
                </div>
              </div>
              <Button variant="ghost" size="icon" aria-label={t("pwd.menu")} title={t("pwd.menu")} onClick={() => navigate("/change-password")} data-testid="change-password-link">
                <KeyRound className="h-[18px] w-[18px]" aria-hidden="true" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("app.logout")}
                title={t("app.logout")}
                onClick={async () => {
                  await logout();
                  navigate("/login");
                }}
                data-testid="logout"
              >
                <LogOut className="h-[18px] w-[18px]" aria-hidden="true" />
              </Button>
            </div>
          </div>
        </header>
        <SubscriptionBanner />
        <main className="min-w-0 flex-1 px-4 pb-6 lg:px-6" lang={i18n.language}>
          <Outlet />
        </main>
      </div>
      <ToastViewport />
    </div>
  );
}
