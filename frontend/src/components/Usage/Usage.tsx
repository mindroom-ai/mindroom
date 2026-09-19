import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownLeft,
  ArrowUpRight,
  BarChart3,
  Database,
  Info,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fetchUsage } from "@/services/usageService";
import type { TokenTotals, UsageReport } from "@/types/usage";
import { UsageActivity } from "./UsageActivity";
import { UsageDetail } from "./UsageDetail";
import {
  getUsageDetail,
  modelRow,
  requesterRow,
  type UsageSelection,
} from "./usageDetails";
import {
  formatTokens,
  UsageMetricSelect,
  UsageTable,
  type UsageTableRow,
} from "./UsageTable";

function UsageReportView({ report }: { report: UsageReport }) {
  const [metric, setMetric] = useState<keyof TokenTotals>("total_tokens");
  const [search, setSearch] = useState("");
  const [selection, setSelection] = useState<UsageSelection | null>(null);
  const detailTrigger = useRef<HTMLButtonElement | null>(null);
  const select =
    (value: UsageSelection): UsageTableRow["onSelect"] =>
    (event) => {
      detailTrigger.current = event.currentTarget;
      setSelection(value);
    };
  const filterRows = (rows: UsageTableRow[]) =>
    rows.filter((row) =>
      `${row.label} ${row.detail ?? ""}`
        .toLowerCase()
        .includes(search.trim().toLowerCase()),
    );
  const entities: UsageTableRow[] = report.breakdown.map((row) => ({
    key: row.key,
    label: row.key,
    totals: row.totals,
    count: row.session_count,
    onSelect: select({ kind: "entity", key: row.key }),
  }));
  const models = report.cumulative_model_breakdown.map((row) => ({
    ...modelRow(row),
    onSelect: select({
      kind: "model",
      provider: row.provider,
      model: row.model,
    }),
  }));
  const requesters = report.user_breakdown.map((row) => ({
    ...requesterRow(row),
    onSelect: select({ kind: "requester", userId: row.user_id }),
  }));
  const unavailable = [
    report.coverage,
    report.cumulative_model_coverage,
    report.user_coverage,
    report.daily_coverage,
  ].some((coverage) => coverage.unavailable_sources > 0);

  return (
    <>
      {unavailable && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm"
        >
          Some usage could not be read. Totals and breakdowns may be incomplete.
        </p>
      )}
      <section aria-label="All-time recorded usage" className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-medium text-muted-foreground">
            All-time recorded usage
          </h2>
          <span className="text-xs text-muted-foreground">
            {formatTokens(report.session_count)} stored sessions
          </span>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[
            {
              label: "Reported tokens",
              value: report.totals.total_tokens,
              icon: BarChart3,
              note: "Provider-reported total",
            },
            {
              label: "Input tokens",
              value: report.totals.input_tokens,
              icon: ArrowDownLeft,
              note: "Prompts and context",
            },
            {
              label: "Output tokens",
              value: report.totals.output_tokens,
              icon: ArrowUpRight,
              note: "Generated output",
            },
            {
              label: "Cache read tokens",
              value: report.totals.cache_read_tokens,
              icon: Database,
              note: `${formatTokens(report.totals.cache_write_tokens)} cache write tokens`,
            },
          ].map(({ label, value, icon: Icon, note }) => (
            <Card key={label}>
              <CardContent className="space-y-3 p-5">
                <div className="flex items-center justify-between text-sm text-muted-foreground">
                  <span>{label}</span>
                  <Icon className="h-4 w-4 text-primary" aria-hidden="true" />
                </div>
                <p className="break-words text-3xl font-semibold tracking-tight tabular-nums">
                  {formatTokens(value)}
                </p>
                <p className="text-xs text-muted-foreground">{note}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>
      <div className="flex items-start gap-2 text-sm text-muted-foreground">
        <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <p>
          Daily and requester views cover recorded runs. Older usage may appear
          only in totals. Token counters follow provider reporting; cache tokens
          may already be included in input.
        </p>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Explore usage</h2>
        <UsageMetricSelect metric={metric} onChange={setMetric} />
      </div>
      <UsageActivity
        daily={report.daily_breakdown}
        generatedAt={report.generated_at}
        metric={metric}
      />
      <Card>
        <Tabs defaultValue="entities">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4">
            <TabsList
              aria-label="Usage breakdown"
              className="flex w-full sm:w-auto"
            >
              <TabsTrigger
                value="entities"
                className="flex-1 px-2 text-xs sm:px-3 sm:text-sm"
              >
                Agents &amp; teams
              </TabsTrigger>
              <TabsTrigger
                value="models"
                className="flex-1 px-2 text-xs sm:px-3 sm:text-sm"
              >
                Models
              </TabsTrigger>
              <TabsTrigger
                value="requesters"
                className="flex-1 px-2 text-xs sm:px-3 sm:text-sm"
              >
                Requesters
              </TabsTrigger>
            </TabsList>
            <Input
              aria-label="Search breakdown"
              placeholder="Search breakdown"
              className="w-full sm:w-56"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <TabsContent value="entities" className="mt-0">
            <p className="px-4 py-3 text-xs text-muted-foreground">
              All-time recorded totals, including shared and private instances.
              Select an agent or team to explore its requesters, models, and
              activity.
            </p>
            <UsageTable
              rows={filterRows(entities)}
              label="Agents & teams"
              metric={metric}
              countLabel="Stored sessions"
            />
          </TabsContent>
          <TabsContent value="models" className="mt-0">
            <p className="px-4 py-3 text-xs text-muted-foreground">
              All-time recorded totals. A session using multiple models appears
              under each model. Unknown means the model was not recorded. Select
              a model to explore its agents, requesters, and activity.
            </p>
            <UsageTable
              rows={filterRows(models)}
              label="Models"
              metric={metric}
              countLabel="Stored sessions"
            />
          </TabsContent>
          <TabsContent value="requesters" className="mt-0">
            <p className="px-4 py-3 text-xs text-muted-foreground">
              Recorded runs only. Requesters identify who triggered a run,
              including on shared agents. Unknown means no requester was
              recorded. Select a requester to explore their agents, models, and
              activity.
            </p>
            <UsageTable
              rows={filterRows(requesters)}
              label="Requesters"
              metric={metric}
              countLabel="Recorded runs"
            />
          </TabsContent>
        </Tabs>
      </Card>
      <UsageDetail
        detail={getUsageDetail(report, selection, metric)}
        generatedAt={report.generated_at}
        metric={metric}
        onMetricChange={setMetric}
        onClose={() => setSelection(null)}
        trigger={detailTrigger}
      />
    </>
  );
}

export function Usage() {
  const query = useQuery({
    queryKey: ["usage"],
    queryFn: ({ signal }) => fetchUsage(signal),
    retry: false,
    staleTime: 60_000,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchInterval: (query) =>
      query.state.status !== "error" && query.state.data?.status === "pending"
        ? query.state.data.retryAfterMs
        : false,
  });
  const report =
    !query.isError && query.data?.status === "ready"
      ? query.data.report
      : undefined;
  const preparing =
    query.isPending || (!query.isError && query.data?.status === "pending");
  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 p-2 pb-8 sm:p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Usage</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Token usage across this deployment
          </p>
          {report && (
            <p className="mt-2 text-xs text-muted-foreground">
              Updated{" "}
              <time dateTime={report.generated_at}>
                {new Date(report.generated_at).toLocaleString()}
              </time>
            </p>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void query.refetch()}
          disabled={query.isFetching || preparing}
        >
          <RefreshCw
            className={`mr-2 h-4 w-4 ${query.isFetching ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          Refresh
        </Button>
      </div>
      {query.isError ? (
        <Card>
          <CardContent className="space-y-4 p-6">
            <p role="alert">{query.error.message}</p>
            <Button variant="outline" onClick={() => void query.refetch()}>
              Try again
            </Button>
          </CardContent>
        </Card>
      ) : preparing ? (
        <Card>
          <CardContent className="flex items-center gap-3 p-8">
            <Loader2
              className="h-5 w-5 animate-spin text-primary"
              aria-hidden="true"
            />
            <div role="status">
              <p className="font-medium">Preparing usage report</p>
              <p className="mt-1 text-sm text-muted-foreground">
                This can take a moment. The page will update automatically.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : report ? (
        <UsageReportView report={report} />
      ) : null}
    </div>
  );
}
