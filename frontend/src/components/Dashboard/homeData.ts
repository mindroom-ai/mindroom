import { useQuery } from "@tanstack/react-query";
import { API_BASE_URL } from "@/lib/api";
import { fetchUsage } from "@/services/usageService";
import { listSchedules } from "@/services/scheduleService";
import type { ScheduleTask } from "@/types/schedule";
import type { UsageDailyRow } from "@/types/usage";

export function buildRunWeek(
  generatedAt: string,
  daily: Pick<UsageDailyRow, "date" | "run_count">[],
  partial: boolean,
) {
  const reportDate = new Date(generatedAt).toISOString().slice(0, 10);
  const end = Date.parse(`${reportDate}T00:00:00Z`);
  const byDay = new Map(daily.map((day) => [day.date, day.run_count]));
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(end - (6 - index) * 86_400_000)
      .toISOString()
      .slice(0, 10);
    return { date, runs: byDay.get(date) ?? (partial ? null : 0) };
  });
}

export type HomeSchedule = Pick<
  ScheduleTask,
  | "task_id"
  | "room_id"
  | "room_alias"
  | "status"
  | "schedule_type"
  | "execute_at"
  | "next_run_at"
  | "description"
  | "message"
>;

export function upcomingSchedules(tasks: HomeSchedule[]) {
  return tasks
    .flatMap((task) => {
      const at =
        task.next_run_at ??
        (task.schedule_type === "once" ? task.execute_at : null);
      if (task.status !== "pending" || !at || !Number.isFinite(Date.parse(at)))
        return [];
      return [{ task, at }];
    })
    .sort((left, right) => Date.parse(left.at) - Date.parse(right.at))
    .slice(0, 3);
}

export function scheduleTime(at: string, timezone: string) {
  const date = new Date(at);
  return {
    date: date.toLocaleDateString("en-US", {
      timeZone: timezone,
      month: "short",
      day: "numeric",
    }),
    time: date.toLocaleTimeString("en-US", {
      timeZone: timezone,
      hour: "numeric",
      minute: "2-digit",
    }),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function isHomeSchedule(value: unknown): value is HomeSchedule {
  return (
    isRecord(value) &&
    typeof value.task_id === "string" &&
    typeof value.room_id === "string" &&
    (value.room_alias === null || typeof value.room_alias === "string") &&
    typeof value.status === "string" &&
    (value.schedule_type === "once" || value.schedule_type === "cron") &&
    (value.execute_at === null || typeof value.execute_at === "string") &&
    (value.next_run_at === null || typeof value.next_run_at === "string") &&
    typeof value.description === "string" &&
    typeof value.message === "string"
  );
}
function isCount(value: unknown): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
export type RuntimeLabel =
  "Idle" | "Working" | "Starting" | "Paused" | "Unavailable";
export async function fetchRuntimeActivity(
  signal?: AbortSignal,
): Promise<RuntimeLabel> {
  const response = await fetch(`${API_BASE_URL}/api/responses/activity`, {
    signal,
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!response.ok && response.status !== 503)
    throw new Error("Runtime unavailable");
  const data: unknown = await response.json();
  if (
    !isRecord(data) ||
    typeof data.runtime_phase !== "string" ||
    (data.admission_paused !== null &&
      typeof data.admission_paused !== "boolean") ||
    (data.active_matrix_operations !== null &&
      !isCount(data.active_matrix_operations)) ||
    !isCount(data.active_openai_requests) ||
    !["idle", "busy", "unavailable"].includes(String(data.status))
  ) {
    throw new Error("Invalid runtime response");
  }
  if (data.runtime_phase === "starting") return "Starting";
  if (data.admission_paused === true) return "Paused";
  if (
    data.runtime_phase !== "ready" ||
    data.admission_paused !== false ||
    data.active_matrix_operations === null
  )
    return "Unavailable";
  return data.status === "idle"
    ? "Idle"
    : data.status === "busy"
      ? "Working"
      : "Unavailable";
}

export function useHomeData() {
  // Match Usage's cache and 202 Retry-After polling contract.
  const usage = useQuery({
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
  const activity = useQuery({
    queryKey: ["home", "activity"],
    queryFn: ({ signal }) => fetchRuntimeActivity(signal),
    retry: false,
    staleTime: 30_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: false,
  });
  const schedules = useQuery({
    queryKey: ["home", "schedules"],
    queryFn: async () => {
      const data: unknown = await listSchedules();
      // Invalid service timezone/data must stay local to this section.
      if (
        !isRecord(data) ||
        !Array.isArray(data.tasks) ||
        !data.tasks.every(isHomeSchedule) ||
        typeof data.timezone !== "string"
      )
        throw new Error("Invalid schedules response");
      new Intl.DateTimeFormat("en-US", { timeZone: data.timezone }).format();
      return { tasks: data.tasks, timezone: data.timezone };
    },
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  return { usage, activity, schedules };
}
