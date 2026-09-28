import { useMemo, useState } from "react";
import { AlertTriangle, BarChart3, Table2, XCircle } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useLots, useOwnerDashboard, type OwnerDashboardOut } from "@/api/hooks";
import { formatMinor } from "@/lib/format";
import { cn } from "@/lib/cn";
import { Card, CardTitle } from "@/components/ui/card";
import { Stat } from "@/components/ui/stat";
import { Segmented, Select } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { Badge, toneFor } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { BarChart, Legend, Meter, type Series } from "@/components/dashboard/charts";

type Board = OwnerDashboardOut;
const PERIODS = ["7", "30", "90"] as const;
const METHODS = ["CASH", "CARD", "ONLINE"] as const;

function localDate(iso: string, lang: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString(lang === "uz" ? "uz-UZ" : "ru-RU", { day: "numeric", month: "long" });
}

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(v >= 0.995 || v === 0 ? 0 : 1)}%`;
}

/** Плитка KPI: крупная сумма без тийинов, миллионы — «3,34 млн»; точное значение — во всплывающей подсказке. */
function compactMoney(minor: number, lang: string): string {
  const sum = minor / 100;
  const loc = lang === "uz" ? "uz-UZ" : "ru-RU";
  const unit = lang === "uz" ? "so‘m" : "сум";
  if (Math.abs(sum) >= 1_000_000) return `${(sum / 1_000_000).toLocaleString(loc, { maximumFractionDigits: 2 })} ${lang === "uz" ? "mln" : "млн"} ${unit}`;
  return `${Math.round(sum).toLocaleString(loc)} ${unit}`;
}

function Delta({ change }: { change: number | null | undefined }) {
  if (change === null || change === undefined) return null;
  const up = change >= 0;
  return (
    <span className={cn("font-sans text-xs font-extrabold", up ? "text-ok-ink" : "text-bad-ink")}>
      {up ? "▲" : "▼"} {Math.abs(change * 100).toFixed(0)}%
    </span>
  );
}

/* Сводка владельца парковки: выручка и сессии за период (сравнение с прошлым), способы оплаты, загрузка по
   часам, площадки, ручные действия операторов, расхождения смен, доступность устройств, лимиты тарифа и сигналы. */
export function DashboardPage() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const [days, setDays] = useState<(typeof PERIODS)[number]>("7");
  const [lotId, setLotId] = useState<string>("");
  const lots = useLots();
  const board = useOwnerDashboard(Number(days), lotId || null);
  const money = (v: number) => formatMinor(v, lang);

  return (
    <div className="space-y-4" data-testid="owner-dashboard">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("nav.dashboard")}</h1>
        {board.data && (
          <span className="text-[13px] font-semibold text-muted" data-testid="dashboard-period">
            {localDate(board.data.period_start, lang)} — {localDate(board.data.period_end, lang)}
          </span>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Select className="h-9 w-auto min-w-[180px] text-[13px]" value={lotId} onChange={(e) => setLotId(e.target.value)} aria-label={t("app.lot")} data-testid="dashboard-lot">
            <option value="">{t("dashboard.allLots")}</option>
            {(lots.data ?? []).map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </Select>
          <Segmented size="sm" ariaLabel={t("dashboard.period")} value={days} onChange={setDays} options={PERIODS.map((p) => ({ value: p, label: fmt(t("dashboard.days"), { n: p }) }))} />
        </div>
      </div>
      {board.isError && <ErrorBox error={board.error} onRetry={() => void board.refetch()} />}
      {board.isPending ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : board.data ? (
        <BoardView b={board.data} money={money} />
      ) : null}
    </div>
  );
}

function BoardView({ b, money }: { b: Board; money: (v: number) => string }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const series: Series[] = [
    { key: "CASH", label: t("dashboard.method.CASH"), fill: "fill-series-1", dot: "bg-series-1" },
    { key: "CARD", label: t("dashboard.method.CARD"), fill: "fill-series-2", dot: "bg-series-2" },
    { key: "ONLINE", label: t("dashboard.method.ONLINE"), fill: "fill-series-3", dot: "bg-series-3" },
  ];
  return (
    <div className="space-y-4">
      <Signals b={b} money={money} />
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label={t("dashboard.revenue")} value={<span title={money(b.revenue.value)}>{compactMoney(b.revenue.value, lang)}</span>} sub={<Delta change={b.revenue.change} />} testId="kpi-revenue" />
        <Stat label={t("dashboard.entries")} value={b.sessions.value} sub={<Delta change={b.sessions.change} />} testId="kpi-sessions" />
        <Stat label={t("dashboard.avgCheck")} value={<span title={money(b.avg_check.value)}>{compactMoney(b.avg_check.value, lang)}</span>} sub={<Delta change={b.avg_check.change} />} testId="kpi-check" />
        <Stat
          label={t("dashboard.avgStay")}
          value={b.avg_minutes !== null && b.avg_minutes !== undefined ? fmt(t("dashboard.minutes"), { n: Math.round(b.avg_minutes) }) : "—"}
          sub={b.review ? fmt(t("dashboard.reviewNow"), { n: b.review }) : undefined}
          tone={b.review ? "warn" : "ink"}
        />
      </div>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <RevenueCard b={b} series={series} money={money} />
        <Card>
          <CardTitle>{t("dashboard.byMethod")}</CardTitle>
          <MethodBreakdown b={b} series={series} money={money} />
          {b.refunds > 0 && <p className="mt-3 text-[12.5px] font-semibold text-muted">{fmt(t("dashboard.refunds"), { sum: money(b.refunds) })}</p>}
        </Card>
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardTitle>{t("dashboard.byHour")}</CardTitle>
          <p className="mb-2 text-[12.5px] font-semibold text-muted">{t("dashboard.byHourHint")}</p>
          <Legend
            series={[
              { key: "in", label: t("dashboard.entriesShort"), fill: "fill-series-1", dot: "bg-series-1" },
              { key: "out", label: t("dashboard.exitsShort"), fill: "fill-series-2", dot: "bg-series-2" },
            ]}
          />
          <BarChart
            ariaLabel={t("dashboard.byHour")}
            testId="chart-hours"
            stacked={false}
            axisEvery={3}
            series={[
              { key: "in", label: t("dashboard.entriesShort"), fill: "fill-series-1", dot: "bg-series-1" },
              { key: "out", label: t("dashboard.exitsShort"), fill: "fill-series-2", dot: "bg-series-2" },
            ]}
            columns={b.load_by_hour.map((h) => ({ label: `${h.hour}`, title: `${String(h.hour).padStart(2, "0")}:00–${String(h.hour).padStart(2, "0")}:59`, values: [h.entries, h.exits] }))}
            format={(v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1))}
          />
        </Card>
        <LotsCard b={b} money={money} />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <ManualCard b={b} />
        <ShiftsCard b={b} money={money} />
      </div>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <DevicesCard b={b} />
        <LimitsCard b={b} />
      </div>
    </div>
  );
}

function Signals({ b, money }: { b: Board; money: (v: number) => string }) {
  const { t } = useTranslation();
  if (b.signals.length === 0) return null;
  const text = (s: Board["signals"][number]) => {
    const p = s.params as Record<string, unknown>;
    switch (s.code) {
      case "CASH_DISCREPANCY":
        return fmt(t("dashboard.signal.cash"), { shifts: String(p.shifts), shortage: money(Number(p.shortage)), surplus: money(Number(p.surplus)) });
      case "MANY_MANUAL_OPENS":
        return fmt(t("dashboard.signal.manual"), { opens: String(p.opens), share: pct(Number(p.share)), operator: String(p.operator ?? "—"), top: String(p.top) });
      case "DEVICE_UNAVAILABLE":
        return fmt(t("dashboard.signal.device"), { devices: (p.devices as string[]).join(", ") });
      case "LIMIT_NEAR":
        return fmt(t("dashboard.signal.limit"), { kind: t(`dashboard.limit.${String(p.kind)}`), used: String(p.used), limit: String(p.limit) });
      default:
        return s.code;
    }
  };
  return (
    <ul className="space-y-2" data-testid="signals">
      {b.signals.map((s, i) => (
        <li
          key={`${s.code}-${i}`}
          data-signal={s.code}
          className={cn("flex items-start gap-2 rounded-xl border px-3 py-2 text-[13.5px] font-semibold", s.severity === "bad" ? "border-bad/40 bg-bad-soft text-bad-ink" : "border-warn/50 bg-warn-soft text-warn-ink")}
        >
          {s.severity === "bad" ? <XCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />}
          <span>
            <b>{t(`dashboard.signal.title.${s.code}`)}</b> — {text(s)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function RevenueCard({ b, series, money }: { b: Board; series: Series[]; money: (v: number) => string }) {
  const { t, i18n } = useTranslation();
  const [table, setTable] = useState(false);
  const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString(i18n.language === "uz" ? "uz-UZ" : "ru-RU", { day: "2-digit", month: "2-digit" });
  const columns = b.revenue_by_day.map((d) => ({ label: day(d.day), title: `${day(d.day)} · ${money(d.total)}`, values: METHODS.map((m) => d.by_method[m] ?? 0) }));
  const compact = (v: number) => {
    const sum = v / 100;
    return sum >= 1_000_000 ? `${(sum / 1_000_000).toFixed(1)}M` : sum >= 1000 ? `${Math.round(sum / 1000)}k` : `${Math.round(sum)}`;
  };
  return (
    <Card>
      <CardTitle
        right={
          <Button size="sm" variant="ghost" onClick={() => setTable(!table)} aria-pressed={table} data-testid="revenue-table-toggle">
            {table ? <BarChart3 className="h-4 w-4" aria-hidden="true" /> : <Table2 className="h-4 w-4" aria-hidden="true" />} {table ? t("dashboard.chart") : t("dashboard.table")}
          </Button>
        }
      >
        {t("dashboard.revenueByDay")}
      </CardTitle>
      <Legend series={series} />
      {table ? (
        <div className="mt-2 max-h-72 overflow-auto">
          <Table data-testid="revenue-table">
            <thead>
              <tr>
                <Th>{t("dashboard.day")}</Th>
                {series.map((s) => (
                  <Th key={s.key} className="text-right">
                    {s.label}
                  </Th>
                ))}
                <Th className="text-right">{t("dashboard.total")}</Th>
              </tr>
            </thead>
            <tbody>
              {b.revenue_by_day.map((d) => (
                <tr key={d.day}>
                  <Td>{day(d.day)}</Td>
                  {METHODS.map((m) => (
                    <Td key={m} mono className="text-right">
                      {money(d.by_method[m] ?? 0)}
                    </Td>
                  ))}
                  <Td mono className="text-right font-bold">
                    {money(d.total)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : (
        <BarChart ariaLabel={t("dashboard.revenueByDay")} testId="chart-revenue" columns={columns} series={series} format={compact} axisEvery={Math.ceil(columns.length / 12)} />
      )}
    </Card>
  );
}

function MethodBreakdown({ b, series, money }: { b: Board; series: Series[]; money: (v: number) => string }) {
  const { t } = useTranslation();
  const total = METHODS.reduce((a, m) => a + (b.revenue_by_method[m] ?? 0), 0);
  const tones = ["series-1", "series-2", "series-3"] as const;
  if (total === 0) return <EmptyState title={t("dashboard.noRevenue")} hint={t("dashboard.noRevenueHint")} />;
  return (
    <ul className="space-y-3" data-testid="methods">
      {METHODS.map((m, i) => {
        const v = b.revenue_by_method[m] ?? 0;
        return (
          <li key={m}>
            <div className="flex items-baseline justify-between gap-2 text-[13.5px] font-bold">
              <span>{series[i]!.label}</span>
              <span className="font-mono tabular-nums">
                {money(v)} <span className="text-muted">· {pct(v / total)}</span>
              </span>
            </div>
            <Meter value={v} max={total} tone={tones[i]} className="mt-1" />
          </li>
        );
      })}
      <li className="flex justify-between border-t border-line pt-2 text-[13px] font-semibold text-muted">
        <span>{t("dashboard.payments")}</span>
        <span className="font-mono">{b.payments}</span>
      </li>
    </ul>
  );
}

function LotsCard({ b, money }: { b: Board; money: (v: number) => string }) {
  const { t } = useTranslation();
  const max = Math.max(1, ...b.lots.map((l) => l.revenue));
  return (
    <Card>
      <CardTitle>{t("dashboard.byLot")}</CardTitle>
      <Table data-testid="lots-report">
        <thead>
          <tr>
            <Th>{t("app.lot")}</Th>
            <Th className="text-right">{t("dashboard.entriesShort")}</Th>
            <Th className="text-right">{t("dashboard.revenue")}</Th>
          </tr>
        </thead>
        <tbody>
          {b.lots.map((l) => (
            <tr key={l.lot_id}>
              <Td>
                <div className="font-bold">{l.name}</div>
                <Meter value={l.revenue} max={max} className="mt-1 max-w-48" />
              </Td>
              <Td mono className="text-right">
                {l.entries}
              </Td>
              <Td mono className="text-right font-bold">
                {money(l.revenue)}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function ManualCard({ b }: { b: Board }) {
  const { t } = useTranslation();
  return (
    <Card>
      <CardTitle>{t("dashboard.manual")}</CardTitle>
      <p className="mb-2 text-[12.5px] font-semibold text-muted">{t("dashboard.manualHint")}</p>
      {b.manual.length === 0 ? (
        <EmptyState title={t("dashboard.manualEmpty")} />
      ) : (
        <div className="overflow-x-auto">
          <Table data-testid="manual-report">
            <thead>
              <tr>
                <Th>{t("dashboard.operator")}</Th>
                <Th className="text-right">{t("dashboard.opens")}</Th>
                <Th className="text-right">{t("dashboard.manualEntries")}</Th>
                <Th className="text-right">{t("dashboard.corrections")}</Th>
                <Th className="text-right">{t("dashboard.closes")}</Th>
              </tr>
            </thead>
            <tbody>
              {b.manual.map((o) => (
                <tr key={o.user_id ?? o.name} data-testid="manual-row">
                  <Td>
                    <div className="font-bold">{o.name}</div>
                    {o.role && <div className="text-[12px] text-muted">{t(`roles.${o.role}`, { defaultValue: o.role })}</div>}
                  </Td>
                  <Td mono className="text-right font-bold">
                    {o.opens}
                  </Td>
                  <Td mono className="text-right">
                    {o.entries}
                  </Td>
                  <Td mono className="text-right">
                    {o.corrections}
                  </Td>
                  <Td mono className="text-right">
                    {o.closes}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

function ShiftsCard({ b, money }: { b: Board; money: (v: number) => string }) {
  const { t, i18n } = useTranslation();
  const when = (iso: string) => new Date(iso).toLocaleString(i18n.language === "uz" ? "uz-UZ" : "ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: b.timezone });
  return (
    <Card>
      <CardTitle>{t("dashboard.shifts")}</CardTitle>
      {b.shifts.length === 0 ? (
        <EmptyState title={t("dashboard.shiftsEmpty")} hint={t("dashboard.shiftsEmptyHint")} />
      ) : (
        <div className="overflow-x-auto">
          <Table data-testid="shifts-report">
            <thead>
              <tr>
                <Th>{t("dashboard.cashier")}</Th>
                <Th>{t("dashboard.closedAt")}</Th>
                <Th className="text-right">{t("dashboard.expected")}</Th>
                <Th className="text-right">{t("dashboard.counted")}</Th>
                <Th className="text-right">{t("dashboard.difference")}</Th>
              </tr>
            </thead>
            <tbody>
              {b.shifts.map((s) => (
                <tr key={s.shift_id} data-testid="shift-row">
                  <Td>
                    <div className="font-bold">{s.cashier_name}</div>
                    <div className="text-[12px] text-muted">{s.lot_name}</div>
                  </Td>
                  <Td>{when(s.closed_at)}</Td>
                  <Td mono className="whitespace-nowrap text-right">
                    {money(s.expected)}
                  </Td>
                  <Td mono className="whitespace-nowrap text-right">
                    {money(s.counted)}
                  </Td>
                  <Td mono className={cn("whitespace-nowrap text-right font-bold", s.discrepancy < 0 ? "text-bad-ink" : "text-warn-ink")}>
                    {s.discrepancy > 0 ? "+" : ""}
                    {money(s.discrepancy)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

function DevicesCard({ b }: { b: Board }) {
  const { t } = useTranslation();
  return (
    <Card>
      <CardTitle>{t("dashboard.devices")}</CardTitle>
      <p className="mb-2 text-[12.5px] font-semibold text-muted">{t("dashboard.devicesHint")}</p>
      <ul className="divide-y divide-line" data-testid="devices-report">
        {b.cameras.map((c) => (
          <li key={c.camera_id} className="grid grid-cols-[minmax(0,1fr)_120px_64px] items-center gap-3 py-2 text-[13.5px]">
            <span className="min-w-0 truncate">
              <b>{c.name}</b> <span className="text-muted">· {c.lot_name}</span>{" "}
              <Badge tone={toneFor(c.status)}>{t(`status.${c.status}`, { defaultValue: c.status })}</Badge>
            </span>
            <Meter value={c.availability ?? 0} max={1} tone={c.availability === null || c.availability === undefined ? "accent" : c.availability >= 0.95 ? "ok" : "warn"} />
            <span className="text-right font-mono font-bold tabular-nums">{pct(c.availability)}</span>
          </li>
        ))}
        {b.barriers.map((br) => (
          <li key={br.barrier_id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 py-2 text-[13.5px]">
            <span className="min-w-0 truncate">
              <b>{br.name}</b> <span className="text-muted">· {br.lot_name}</span>
            </span>
            <span className={cn("font-mono text-[12.5px] font-bold tabular-nums", br.failed > 0 && "text-bad-ink", br.commands === 0 && "text-muted")}>
              {br.commands === 0 ? t("dashboard.noCommands") : fmt(t("dashboard.barrierStats"), { n: br.commands, failed: br.failed, ms: br.avg_latency_ms ?? "—" })}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function LimitsCard({ b }: { b: Board }) {
  const { t } = useTranslation();
  const rows = useMemo(() => b.limits, [b.limits]);
  return (
    <Card>
      <CardTitle>{fmt(t("dashboard.limits"), { plan: b.plan_name })}</CardTitle>
      <ul className="space-y-3" data-testid="limits">
        {rows.map((l) => {
          const share = l.limit ? l.used / l.limit : 0;
          return (
            <li key={l.kind}>
              <div className="flex justify-between text-[13.5px] font-bold">
                <span>{t(`dashboard.limit.${l.kind}`)}</span>
                <span className="font-mono tabular-nums">
                  {l.used} / {l.limit ?? "∞"}
                </span>
              </div>
              {l.limit ? <Meter value={l.used} max={l.limit} tone={share >= 1 ? "bad" : share >= 0.9 ? "warn" : "ok"} className="mt-1" /> : null}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
