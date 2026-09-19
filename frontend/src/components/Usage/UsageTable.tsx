import type { MouseEventHandler } from "react";
import type { TokenTotals } from "@/types/usage";

export const TOKEN_METRICS: Record<keyof TokenTotals, string> = {
  total_tokens: "Reported tokens",
  input_tokens: "Input tokens",
  output_tokens: "Output tokens",
  cache_read_tokens: "Cache read tokens",
  cache_write_tokens: "Cache write tokens",
  reasoning_tokens: "Reasoning tokens",
  audio_input_tokens: "Audio input tokens",
  audio_output_tokens: "Audio output tokens",
  audio_total_tokens: "Audio total tokens",
};

export const formatTokens = (value: number) => value.toLocaleString();

export function UsageMetricSelect({
  metric,
  onChange,
}: {
  metric: keyof TokenTotals;
  onChange: (metric: keyof TokenTotals) => void;
}) {
  return (
    <label className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
      Token metric
      <select
        value={metric}
        onChange={(event) => onChange(event.target.value as keyof TokenTotals)}
        className="max-w-52 rounded-md border bg-background px-3 py-2 text-foreground"
      >
        {Object.entries(TOKEN_METRICS).map(([key, label]) => (
          <option key={key} value={key}>
            {label}
          </option>
        ))}
      </select>
    </label>
  );
}

export interface UsageTableRow {
  key: string;
  label: string;
  detail?: string;
  totals: TokenTotals;
  count: number;
  onSelect?: MouseEventHandler<HTMLButtonElement>;
}

export function UsageTable({
  rows,
  label,
  metric,
  countLabel,
}: {
  rows: UsageTableRow[];
  label: string;
  metric: keyof TokenTotals;
  countLabel: string;
}) {
  const sorted = [...rows].sort(
    (a, b) =>
      b.totals[metric] - a.totals[metric] || a.label.localeCompare(b.label),
  );
  const maximum = Math.max(1, ...rows.map((row) => row.totals[metric]));
  return (
    <div className="overflow-x-auto">
      <table aria-label={label} className="w-full text-sm">
        <thead className="border-b text-xs text-muted-foreground">
          <tr>
            <th scope="col" className="px-2 py-3 text-left font-medium sm:px-4">
              {label}
            </th>
            <th
              scope="col"
              className="px-2 py-3 text-right font-medium sm:px-4"
            >
              {countLabel}
            </th>
            <th
              scope="col"
              className="w-2/5 min-w-24 px-2 py-3 text-right font-medium sm:min-w-36 sm:px-4"
            >
              {TOKEN_METRICS[metric]}
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/50">
          {sorted.map((row) => (
            <tr key={row.key} className="hover:bg-muted/40">
              <th
                scope="row"
                className="max-w-80 px-2 py-4 text-left font-medium [overflow-wrap:anywhere] sm:px-4"
              >
                {row.onSelect ? (
                  <button
                    onClick={row.onSelect}
                    aria-label={
                      row.detail ? `${row.label} (${row.detail})` : undefined
                    }
                    className="text-primary underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-primary"
                  >
                    {row.label}
                  </button>
                ) : (
                  row.label
                )}
                {row.detail && (
                  <div className="mt-1 text-xs font-normal text-muted-foreground">
                    {row.detail}
                  </div>
                )}
              </th>
              <td className="px-2 py-4 text-right tabular-nums text-muted-foreground sm:px-4">
                {formatTokens(row.count)}
              </td>
              <td className="px-2 py-4 text-right tabular-nums sm:px-4">
                {formatTokens(row.totals[metric])}
                <div
                  aria-hidden="true"
                  className="mt-2 h-1 rounded-full bg-muted"
                >
                  <div
                    className="h-full rounded-full bg-primary/70"
                    style={{
                      width: `${(row.totals[metric] / maximum) * 100}%`,
                    }}
                  />
                </div>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td
                colSpan={3}
                className="px-4 py-10 text-center text-muted-foreground"
              >
                No matching usage.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
