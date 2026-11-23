import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Clock, Loader2, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  fetchProviderLimits,
  ProviderLimitsHttpError,
} from "@/services/usageService";
import type {
  ProviderLimitAccount,
  ProviderLimitWindow,
  ProviderUnavailableReason,
} from "@/types/usage";
import { formatTokens } from "./UsageTable";

const UNAVAILABLE_COPY: Record<ProviderUnavailableReason, string> = {
  not_signed_in: "Not signed in",
  probe_failed: "Could not check provider limits",
  unsupported: "Live limit checks unavailable for this credential",
};

function formatPercent(value: number): string {
  const clamped = Math.max(0, Math.min(100, value));
  return Number.isInteger(clamped) ? `${clamped}%` : `${clamped.toFixed(1)}%`;
}

function formatUsd(cost: number, estimated: boolean): string {
  const prefix = estimated ? "~$" : "$";
  return cost > 0 && cost < 0.01 ? `${prefix}<0.01` : `${prefix}${cost.toFixed(2)}`;
}

function formatResetCountdown(resetsAt: string, nowMs: number): string | null {
  const targetMs = Date.parse(resetsAt);
  if (!Number.isFinite(targetMs)) return null;
  const diffMs = targetMs - nowMs;
  if (diffMs <= 0) return null;
  const diffMins = Math.round(diffMs / 60_000);
  if (diffMins <= 1) return "Resets soon";
  if (diffMins < 60) return `Resets in ${diffMins}m`;
  const hours = Math.floor(diffMins / 60);
  const mins = diffMins % 60;
  if (hours < 24) {
    return mins > 0 ? `Resets in ${hours}h ${mins}m` : `Resets in ${hours}h`;
  }
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours > 0 ? `Resets in ${days}d ${remHours}h` : `Resets in ${days}d`;
}

function paceRemainingPercent(
  window: ProviderLimitWindow,
  nowMs: number,
): number | null {
  if (!window.window_duration_mins || !window.resets_at) return null;
  const totalMs = window.window_duration_mins * 60_000;
  const remainingMs = Date.parse(window.resets_at) - nowMs;
  if (!Number.isFinite(remainingMs) || remainingMs <= 0 || remainingMs >= totalMs) {
    return null;
  }
  return Math.max(0, Math.min(100, (remainingMs / totalMs) * 100));
}

function barFillClass(remaining: number): string {
  if (remaining <= 15) return "bg-rose-500";
  if (remaining <= 35) return "bg-amber-500";
  return "bg-emerald-500";
}

function LimitWindowRow({
  window,
  nowMs,
}: {
  window: ProviderLimitWindow;
  nowMs: number;
}) {
  const remaining = Math.max(0, Math.min(100, window.remaining_percent));
  const paceLeft = paceRemainingPercent(window, nowMs);
  const countdown = window.resets_at
    ? formatResetCountdown(window.resets_at, nowMs)
    : null;

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
        <span className="font-medium text-foreground">{window.label}</span>
        <div className="flex flex-wrap items-center gap-2 text-muted-foreground tabular-nums">
          <span className="font-medium text-foreground">
            {formatPercent(remaining)} left
          </span>
          {window.detail && <span>{window.detail}</span>}
          {countdown && (
            <span className="inline-flex items-center gap-1">
              <Clock className="h-3 w-3" aria-hidden="true" />
              {countdown}
            </span>
          )}
        </div>
      </div>
      <div
        role="progressbar"
        aria-label={window.label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(remaining)}
        className="relative h-2 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={`h-full rounded-full transition-all ${barFillClass(remaining)}`}
          style={{ width: `${remaining}%` }}
        />
        {paceLeft !== null && (
          <div
            data-testid="pace-tick"
            title={`Expected remaining at steady pace: ${Math.round(paceLeft)}%`}
            className="pointer-events-none absolute inset-y-0 w-0.5 -translate-x-1/2 bg-foreground/75"
            style={{ left: `${paceLeft}%` }}
          />
        )}
      </div>
    </div>
  );
}

function AccountCard({
  account,
  nowMs,
}: {
  account: ProviderLimitAccount;
  nowMs: number;
}) {
  const planLabel = account.plan ?? account.account_label;
  const showStale =
    account.stale && account.unavailable_reason !== "not_signed_in";
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="space-y-0.5">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold">{account.display_name}</h3>
              {planLabel && (
                <Badge variant="secondary" className="text-xs font-normal">
                  {planLabel}
                </Badge>
              )}
              {showStale && (
                <Badge
                  variant="outline"
                  className="border-amber-500/40 bg-amber-500/10 text-xs text-amber-600 dark:text-amber-400"
                >
                  Stale
                </Badge>
              )}
            </div>
            {account.model_keys.length > 0 && (
              <p className="text-xs text-muted-foreground">
                {account.model_keys.join(", ")}
              </p>
            )}
          </div>
          <div className="text-right text-xs tabular-nums">
            <p className="font-medium">
              {formatTokens(account.tokens_7d)} tokens
            </p>
            <p className="text-muted-foreground">
              {account.cost_usd_7d !== null
                ? `${formatUsd(account.cost_usd_7d, account.cost_estimated)} · 7d`
                : "Last 7 days"}
            </p>
          </div>
        </div>

        {account.windows.length > 0 && (
          <div className="space-y-2.5">
            {account.windows.map((window) => (
              <LimitWindowRow key={window.id} window={window} nowMs={nowMs} />
            ))}
          </div>
        )}

        {showStale && (
          <p className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>Showing cached reading after a failed refresh.</span>
          </p>
        )}

        {account.unavailable_reason && (
          <p className="text-xs text-muted-foreground">
            {UNAVAILABLE_COPY[account.unavailable_reason]}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function ProviderLimits() {
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const query = useQuery({
    queryKey: ["provider-limits"],
    queryFn: ({ signal }) => fetchProviderLimits(false, signal),
    enabled: authError === null,
    retry: false,
    staleTime: 60_000,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });

  const handleRefresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    setAuthError(null);
    try {
      const fresh = await fetchProviderLimits(true);
      queryClient.setQueryData(["provider-limits"], fresh);
      setNowMs(Date.now());
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Could not refresh provider limits.";
      if (
        error instanceof ProviderLimitsHttpError &&
        (error.status === 401 || error.status === 403)
      ) {
        queryClient.setQueryData(["provider-limits"], undefined);
        setAuthError(message);
      } else {
        setRefreshError(message);
      }
    } finally {
      setRefreshing(false);
    }
  };

  const handleRetry = () => {
    const hadAuthError = authError !== null;
    setAuthError(null);
    setRefreshError(null);
    if (!hadAuthError) {
      void query.refetch();
    }
  };

  const snapshot = authError ? undefined : query.data;
  const blockingError = authError ?? (query.isError ? query.error.message : null);
  const busy = query.isFetching || refreshing;

  return (
    <section aria-label="Provider limits" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="text-sm font-medium text-muted-foreground">
            Provider limits
          </h2>
          {snapshot && (
            <span className="text-xs text-muted-foreground">
              Checked{" "}
              <time dateTime={snapshot.checked_at}>
                {new Date(snapshot.checked_at).toLocaleTimeString()}
              </time>
            </span>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void handleRefresh()}
          disabled={busy}
        >
          <RefreshCw
            className={`mr-2 h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          Refresh limits
        </Button>
      </div>

      {refreshError && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs"
        >
          {refreshError}
        </p>
      )}

      {blockingError ? (
        <Card>
          <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
            <p role="alert" className="text-sm text-muted-foreground">
              {blockingError}
            </p>
            <Button variant="outline" size="sm" onClick={handleRetry}>
              Try again
            </Button>
          </CardContent>
        </Card>
      ) : query.isPending ? (
        <Card>
          <CardContent className="flex items-center gap-2.5 p-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            <span>Checking provider limits…</span>
          </CardContent>
        </Card>
      ) : snapshot && snapshot.accounts.length > 0 ? (
        <>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {snapshot.accounts.map((account) => (
              <AccountCard key={account.id} account={account} nowMs={nowMs} />
            ))}
          </div>
          {snapshot.unattributed_tokens_7d > 0 && (
            <p className="text-xs text-muted-foreground">
              {formatTokens(snapshot.unattributed_tokens_7d)} additional tokens
              in the last 7 days could not be uniquely attributed to one account
              {snapshot.unattributed_cost_usd_7d !== null
                ? ` (${formatUsd(snapshot.unattributed_cost_usd_7d, true)})`
                : ""}
              .
            </p>
          )}
        </>
      ) : (
        <Card>
          <CardContent className="p-4 text-sm text-muted-foreground">
            No configured provider accounts.
          </CardContent>
        </Card>
      )}
    </section>
  );
}
