import { useId, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TokenTotals, UsageDailyRow } from "@/types/usage";
import { formatTokens, TOKEN_METRICS } from "./UsageTable";

const DAY_MS = 86_400_000;

export function UsageActivity({
  daily,
  generatedAt,
  metric,
}: {
  daily: UsageDailyRow[];
  generatedAt: string;
  metric: keyof TokenTotals;
}) {
  const [period, setPeriod] = useState("30");
  const id = useId();
  const end = Date.parse(`${generatedAt.slice(0, 10)}T00:00:00Z`);
  const start =
    period === "all"
      ? Math.min(
          end,
          ...daily.map((row) => Date.parse(`${row.date}T00:00:00Z`)),
        )
      : end - (Number(period) - 1) * DAY_MS;
  const rows = daily
    .filter((row) => {
      const day = Date.parse(`${row.date}T00:00:00Z`);
      return day >= start && day <= end;
    })
    .sort((a, b) => a.date.localeCompare(b.date));
  const days = (end - start) / DAY_MS + 1;
  const maximum = Math.max(1, ...rows.map((row) => row.totals[metric]));
  const sum = rows.reduce((total, row) => total + row.totals[metric], 0);
  return (
    <section aria-label="Daily activity">
      <Card>
        <CardHeader className="flex-row flex-wrap items-start justify-between gap-4 space-y-0">
          <div className="space-y-2">
            <CardTitle>Daily activity</CardTitle>
            <p className="text-sm text-muted-foreground">
              Recorded runs, by UTC day. Gaps have no recorded detail.
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="sr-only">Activity period</span>
            <select
              value={period}
              onChange={(event) => setPeriod(event.target.value)}
              className="rounded-md border bg-background px-3 py-2 text-foreground"
            >
              <option value="7">Last 7 days</option>
              <option value="30">Last 30 days</option>
              <option value="90">Last 90 days</option>
              <option value="all">All recorded days</option>
            </select>
          </label>
        </CardHeader>
        <CardContent>
          <div className="mb-4 flex flex-wrap items-baseline gap-2">
            <span className="text-3xl font-semibold tracking-tight tabular-nums">
              {formatTokens(sum)}
            </span>
            <span className="text-sm text-muted-foreground">
              {TOKEN_METRICS[metric].toLowerCase()} in this period
            </span>
          </div>
          {rows.length === 0 ? (
            <p className="py-12 text-center text-sm text-muted-foreground">
              No recorded daily activity in this period.
            </p>
          ) : (
            <>
              <div className="flex gap-3 pt-2">
                <div
                  aria-hidden="true"
                  className="flex h-36 w-10 shrink-0 flex-col justify-between text-right text-xs text-muted-foreground tabular-nums"
                >
                  {[1, 0.5, 0].map((ratio) => (
                    <span key={ratio}>
                      {Math.round(maximum * ratio).toLocaleString(undefined, {
                        notation: "compact",
                      })}
                    </span>
                  ))}
                </div>
                <div className="min-w-0 flex-1">
                  <svg
                    viewBox="0 0 800 144"
                    preserveAspectRatio="none"
                    className="h-36 w-full"
                    role="img"
                    aria-labelledby={id}
                  >
                    <title id={id}>
                      {TOKEN_METRICS[metric]} by UTC day. Exact values are
                      available in the daily data table.
                    </title>
                    {[0, 0.5, 1].map((ratio) => (
                      <line
                        key={ratio}
                        x1="0"
                        x2="800"
                        y1={143 - ratio * 142}
                        y2={143 - ratio * 142}
                        className="stroke-border"
                      />
                    ))}
                    {rows.map((row) => {
                      const x =
                        (((Date.parse(`${row.date}T00:00:00Z`) - start) /
                          DAY_MS +
                          0.5) /
                          days) *
                        800;
                      const height = (row.totals[metric] / maximum) * 142;
                      const width = Math.max(
                        1,
                        Math.min(32, (800 / days) * 0.7),
                      );
                      return (
                        <rect
                          key={row.date}
                          x={x - width / 2}
                          y={143 - height}
                          width={width}
                          height={height}
                          rx="2"
                          className="fill-primary/80 hover:fill-primary"
                        >
                          <title>
                            {row.date}: {formatTokens(row.totals[metric])}
                          </title>
                        </rect>
                      );
                    })}
                  </svg>
                  <div className="mt-2 flex flex-wrap justify-between gap-2 text-xs text-muted-foreground">
                    <span>{new Date(start).toISOString().slice(0, 10)}</span>
                    <span>{generatedAt.slice(0, 10)}</span>
                  </div>
                </div>
              </div>
              <details className="mt-4 border-t pt-3 text-sm">
                <summary className="w-fit cursor-pointer text-muted-foreground hover:text-foreground">
                  View daily data
                </summary>
                <div className="mt-3 max-h-64 overflow-auto">
                  <table
                    aria-label="Daily data"
                    className="w-full text-left text-sm tabular-nums"
                  >
                    <thead>
                      <tr className="text-muted-foreground">
                        <th scope="col" className="py-2 font-medium">
                          Date (UTC)
                        </th>
                        <th scope="col" className="text-right font-medium">
                          Recorded runs
                        </th>
                        <th scope="col" className="text-right font-medium">
                          {TOKEN_METRICS[metric]}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <tr key={row.date} className="border-t">
                          <th scope="row" className="py-2 font-normal">
                            {row.date}
                          </th>
                          <td className="text-right">
                            {formatTokens(row.run_count)}
                          </td>
                          <td className="text-right">
                            {formatTokens(row.totals[metric])}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
