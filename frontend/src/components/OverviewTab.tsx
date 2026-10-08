import { useCallback, useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ApiError,
  fetchOverview,
  type OverviewQuery,
  type OverviewResponse,
} from "../lib/api";
import { ErrorState } from "./ErrorState";
import { Activity, MessageSquare, RefreshCw, Users } from "lucide-react";
import { InfoTooltip } from "./InfoTooltip";
import {
  RangePicker,
} from "./overview/RangePicker";
import { clampToToday, rangeLabel, type OverviewRange } from "./overview/range";
import { OVERVIEW_TOOLTIPS } from "./overview/copy";

const STORAGE_KEY = "agent-app-orchestrator.overview.range";
const DEFAULT_RANGE: OverviewRange = { preset: "30d" };

function readStoredRange(): OverviewRange {
  if (typeof window === "undefined") return DEFAULT_RANGE;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_RANGE;
    const parsed = JSON.parse(raw) as Partial<OverviewRange>;
    if (
      parsed &&
      typeof parsed.preset === "string" &&
      ["today", "7d", "30d", "90d", "custom"].includes(parsed.preset)
    ) {
      // A previous session may have persisted a future date (Bug 1 — before
      // the picker capped at today). Clamp on load so the picker and the
      // overview query never start with a future bound.
      return {
        preset: parsed.preset as OverviewRange["preset"],
        from: clampToToday(parsed.from),
        to: clampToToday(parsed.to),
      };
    }
  } catch {
    // Treat any parse error as missing preference.
  }
  return DEFAULT_RANGE;
}

function writeStoredRange(range: OverviewRange): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(range));
  } catch {
    // Ignore quota / disabled-storage errors; range is non-essential state.
  }
}

function rangeToQuery(range: OverviewRange): OverviewQuery {
  switch (range.preset) {
    case "today":
      return { days: 1 };
    case "7d":
      return { days: 7 };
    case "30d":
      return { days: 30 };
    case "90d":
      return { days: 90 };
    case "custom":
      if (range.from && range.to) return { from: range.from, to: range.to };
      return { days: 30 };
  }
}

interface StatCardProps {
  label: string;
  value: string | number;
  hint?: string;
  icon?: React.ReactNode;
  tooltip?: { label: string; description: string };
}

function StatCard({ label, value, hint, icon, tooltip }: StatCardProps) {
  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
          {tooltip && (
            <InfoTooltip label={tooltip.label} description={tooltip.description} />
          )}
        </span>
        {icon && <span className="text-muted-foreground">{icon}</span>}
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight">{value}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

export function OverviewTab() {
  const [range, setRange] = useState<OverviewRange>(() => readStoredRange());
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; status?: number } | null>(null);

  const load = useCallback(
    (current: OverviewRange) => {
      const ctrl = new AbortController();
      setLoading(true);
      setError(null);
      fetchOverview(rangeToQuery(current), ctrl.signal)
        .then((res) => {
          setData(res);
          setLoading(false);
        })
        .catch((err) => {
          if (ctrl.signal.aborted) return;
          if (err instanceof ApiError) {
            setError({ status: err.status, message: err.message });
          } else {
            setError({ message: (err as Error).message });
          }
          setLoading(false);
        });
      return () => ctrl.abort();
    },
    [],
  );

  useEffect(() => {
    writeStoredRange(range);
    const cancel = load(range);
    return cancel;
  }, [range, load]);

  if (error) {
    if (error.status === 403) {
      return (
        <ErrorState
          message="Access denied"
          hint="The dashboard requires the Admin app role. Sign in with an admin account or contact the workspace owner to assign the role."
        />
      );
    }
    if (error.status === 401) {
      return (
        <ErrorState
          message="Sign-in required"
          hint="The orchestrator API requires a valid bearer token. Make sure your client requests a token with the api://<client_id>/... scope."
        />
      );
    }
    return <ErrorState message="Failed to load overview" hint={error.message} />;
  }

  // First load: nothing to show yet, render the spinner. After data has
  // arrived once, keep the chart and KPIs mounted during refreshes (range
  // changes, custom date edits) and surface the in-flight state with a small
  // inline indicator instead of zeroing everything out (#241 follow-up:
  // clicking "Custom range" used to unmount the chart on every keystroke).
  if (!data) {
    return (
      <div className="flex items-center justify-center rounded-lg border bg-card p-12 text-sm text-muted-foreground">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
        Loading overview...
      </div>
    );
  }

  const activeLabel = rangeLabel(range);

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">Overview</h1>
            <p className="text-xs text-muted-foreground">
              Active window: {activeLabel} (UTC). The four KPI cards use their
              own fixed windows.
            </p>
          </div>
          {loading && (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <RefreshCw className="h-3 w-3 animate-spin" />
              Refreshing...
            </span>
          )}
        </div>
        <RangePicker value={range} onChange={setRange} />
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Today"
          value={data.today}
          hint="Conversations in the last 24h"
          icon={<MessageSquare className="h-4 w-4" />}
          tooltip={{ label: "Today", description: OVERVIEW_TOOLTIPS.today }}
        />
        <StatCard
          label="Last 7 days"
          value={data.last_7_days}
          hint="Conversations created"
          icon={<MessageSquare className="h-4 w-4" />}
          tooltip={{ label: "Last 7 days", description: OVERVIEW_TOOLTIPS.last7Days }}
        />
        <StatCard
          label="Last 30 days"
          value={data.last_30_days}
          hint={`out of ${data.total} total`}
          icon={<MessageSquare className="h-4 w-4" />}
          tooltip={{ label: "Last 30 days", description: OVERVIEW_TOOLTIPS.last30Days }}
        />
        <StatCard
          label="Active users"
          value={data.active_users}
          hint={`Distinct, ${activeLabel}`}
          icon={<Users className="h-4 w-4" />}
          tooltip={{ label: "Active users", description: OVERVIEW_TOOLTIPS.activeUsers }}
        />
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <div className="rounded-lg border bg-card p-4 shadow-sm lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-1 text-sm font-semibold">
              Conversations over time
              <InfoTooltip
                label="Conversations over time"
                description={OVERVIEW_TOOLTIPS.conversationsOverTime}
              />
            </h2>
            <span className="text-xs text-muted-foreground">
              {activeLabel}, UTC
            </span>
          </div>
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data.conversations_per_day}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis
                  dataKey="date"
                  tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
                  tickFormatter={(v: string) => v.slice(5)}
                  minTickGap={20}
                />
                <YAxis
                  tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
                  allowDecimals={false}
                />
                <Tooltip
                  contentStyle={{
                    background: "hsl(var(--card))",
                    border: "1px solid hsl(var(--border))",
                    fontSize: 12,
                  }}
                  labelStyle={{ color: "hsl(var(--foreground))" }}
                />
                <Line
                  type="monotone"
                  dataKey="count"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="rounded-lg border bg-card p-4 shadow-sm">
          <h2 className="mb-3 flex items-center gap-1 text-sm font-semibold">
            Engagement
            <InfoTooltip
              label="Engagement"
              description={OVERVIEW_TOOLTIPS.engagement}
            />
          </h2>
          <div className="space-y-4">
            <div className="flex items-baseline justify-between">
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                Average turns per conversation
                <InfoTooltip
                  label="Average turns per conversation"
                  description={OVERVIEW_TOOLTIPS.avgTurns}
                />
              </span>
              <span className="text-lg font-semibold tabular-nums">{data.avg_turns}</span>
            </div>
            <div className="flex items-baseline justify-between">
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                Active users (window)
                <InfoTooltip
                  label="Active users (window)"
                  description={OVERVIEW_TOOLTIPS.activeUsersWindow}
                />
              </span>
              <span className="text-lg font-semibold tabular-nums">{data.active_users}</span>
            </div>
            <div className="flex items-baseline justify-between">
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                Total conversations
                <InfoTooltip
                  label="Total conversations"
                  description={OVERVIEW_TOOLTIPS.totalConversations}
                />
              </span>
              <span className="text-lg font-semibold tabular-nums">
                {data.in_window_count ?? data.total}
              </span>
            </div>
            <div className="flex items-center gap-1.5 border-t pt-3 text-xs text-muted-foreground">
              <Activity className="h-3 w-3" />
              <span>Cached for 60 seconds</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
