import type {
  TokenTotals,
  UsageCumulativeModelRow,
  UsageDailyRow,
  UsageModelRow,
  UsageReport,
  UsageUserRow,
} from "@/types/usage";
import { formatTokens, TOKEN_METRICS, type UsageTableRow } from "./UsageTable";

export type UsageSelection =
  | { kind: "entity"; key: string }
  | { kind: "requester"; userId: string | null }
  | { kind: "model"; provider: string; model: string };

type ActivityRow = Pick<UsageDailyRow, "date" | "totals" | "run_count">;

interface DetailSection {
  label: string;
  rows: UsageTableRow[];
  countLabel: string;
  note: string;
}

export interface UsageDetailData {
  title: string;
  subtitle: string;
  totals: TokenTotals;
  count: number;
  countLabel: string;
  basis: "All-time recorded usage" | "Recorded runs only";
  sections: DetailSection[];
  daily: ActivityRow[];
}

const RECORDED_NOTE =
  "Recorded runs only. Older usage may lack requester, model, or daily detail.";
const SESSION_MODEL_NOTE =
  "All-time recorded totals. A session using multiple models appears under each model.";

export function requesterRow(
  row: Pick<UsageUserRow, "user_id" | "totals" | "run_count">,
): UsageTableRow {
  return {
    key: JSON.stringify(row.user_id),
    label: row.user_id ?? "Unknown requester",
    totals: row.totals,
    count: row.run_count,
  };
}

export function modelRow(
  row: UsageModelRow | UsageCumulativeModelRow,
): UsageTableRow {
  return {
    key: JSON.stringify([row.provider, row.model]),
    label: row.model || "Unknown model",
    detail: row.provider || "Unknown provider",
    totals: row.totals,
    count: "session_count" in row ? row.session_count : row.run_count,
  };
}

// Requesters partition an entity's runs. Model rows do not: one run can use
// several models, so summing their counts would overcount daily activity.
function entityDaily(users: UsageUserRow[]): ActivityRow[] {
  const days = new Map<string, ActivityRow>();
  for (const user of users) {
    for (const row of user.daily_breakdown) {
      const existing = days.get(row.date);
      if (existing) {
        for (const metric of Object.keys(
          TOKEN_METRICS,
        ) as (keyof TokenTotals)[]) {
          existing.totals[metric] += row.totals[metric];
        }
        existing.run_count += row.run_count;
      } else {
        days.set(row.date, {
          date: row.date,
          totals: { ...row.totals },
          run_count: row.run_count,
        });
      }
    }
  }
  return [...days.values()];
}

export function getUsageDetail(
  report: UsageReport,
  selection: UsageSelection | null,
  metric: keyof TokenTotals,
): UsageDetailData | undefined {
  if (!selection) return;

  if (selection.kind === "entity") {
    const entity = report.breakdown.find((row) => row.key === selection.key);
    if (!entity) return;
    return {
      title: entity.key,
      subtitle: "Agent or team · Includes shared and private instances",
      totals: entity.totals,
      count: entity.session_count,
      countLabel: "stored sessions",
      basis: "All-time recorded usage",
      sections: [
        {
          label: "Requesters",
          rows: entity.user_breakdown.map(requesterRow),
          countLabel: "Recorded runs",
          note: `${formatTokens(entity.retained_run_totals[metric])} tokens across ${formatTokens(entity.run_count)} recorded runs. ${RECORDED_NOTE}`,
        },
        {
          label: "Models",
          rows: entity.cumulative_model_breakdown.map(modelRow),
          countLabel: "Stored sessions",
          note: SESSION_MODEL_NOTE,
        },
      ],
      daily: entityDaily(entity.user_breakdown),
    };
  }

  if (selection.kind === "requester") {
    const user = report.user_breakdown.find(
      (row) => row.user_id === selection.userId,
    );
    if (!user) return;
    return {
      title: user.user_id ?? "Unknown requester",
      subtitle: "Requester · Who triggered the recorded runs",
      totals: user.totals,
      count: user.run_count,
      countLabel: "recorded runs",
      basis: "Recorded runs only",
      sections: [
        {
          label: "Agents & teams",
          rows: report.breakdown.flatMap((entity) => {
            const usage = entity.user_breakdown.find(
              (row) => row.user_id === user.user_id,
            );
            return usage
              ? [
                  {
                    key: entity.key,
                    label: entity.key,
                    totals: usage.totals,
                    count: usage.run_count,
                  },
                ]
              : [];
          }),
          countLabel: "Recorded runs",
          note: RECORDED_NOTE,
        },
        {
          label: "Models",
          rows: user.model_breakdown.map(modelRow),
          countLabel: "Recorded runs",
          note: "Recorded runs only. A run using multiple models appears under each model.",
        },
      ],
      daily: user.daily_breakdown,
    };
  }

  const matches = (row: { provider: string; model: string }) =>
    row.provider === selection.provider && row.model === selection.model;
  const model = report.cumulative_model_breakdown.find(matches);
  if (!model) return;
  return {
    title: model.model || "Unknown model",
    subtitle: `Model · ${model.provider || "Unknown provider"}`,
    totals: model.totals,
    count: model.session_count,
    countLabel: "stored sessions",
    basis: "All-time recorded usage",
    sections: [
      {
        label: "Agents & teams",
        rows: report.breakdown.flatMap((entity) => {
          const usage = entity.cumulative_model_breakdown.find(matches);
          return usage
            ? [
                {
                  key: entity.key,
                  label: entity.key,
                  totals: usage.totals,
                  count: usage.session_count,
                },
              ]
            : [];
        }),
        countLabel: "Stored sessions",
        note: "All-time recorded totals for this provider and model.",
      },
      {
        label: "Requesters",
        rows: report.user_breakdown.flatMap((user) => {
          const usage = user.model_breakdown.find(matches);
          return usage
            ? [
                requesterRow({
                  user_id: user.user_id,
                  totals: usage.totals,
                  run_count: usage.run_count,
                }),
              ]
            : [];
        }),
        countLabel: "Recorded runs",
        note: RECORDED_NOTE,
      },
    ],
    daily: report.daily_breakdown.flatMap((day) => {
      const usage = day.model_breakdown.find(matches);
      return usage
        ? [{ date: day.date, totals: usage.totals, run_count: usage.run_count }]
        : [];
    }),
  };
}
