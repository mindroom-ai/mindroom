import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildRunWeek,
  fetchRuntimeActivity,
  upcomingSchedules,
  scheduleTime,
} from "./homeData";
import type { ScheduleTask } from "@/types/schedule";

const task = (
  id: string,
  overrides: Partial<ScheduleTask> = {},
): ScheduleTask => ({
  task_id: id,
  room_id: "!room:server",
  room_alias: null,
  status: "pending",
  schedule_type: "once",
  execute_at: "2026-09-20T12:00:00Z",
  next_run_at: null,
  cron_expression: null,
  cron_description: null,
  description: id,
  message: id,
  history_limit: null,
  thread_id: null,
  new_thread: false,
  silent: false,
  created_by: null,
  created_at: null,
  ...overrides,
});
afterEach(() => vi.unstubAllGlobals());
describe("Home data", () => {
  it("anchors seven UTC days to report time, excludes future and old rows, and uses runs", () => {
    const days = buildRunWeek(
      "2026-09-19T23:30:00-07:00",
      [
        { date: "2026-09-13", run_count: 900 },
        { date: "2026-09-14", run_count: 2 },
        { date: "2026-09-20", run_count: 17 },
        { date: "2026-09-21", run_count: 999 },
      ],
      false,
    );
    expect(days.map((day) => day.date)).toEqual([
      "2026-09-14",
      "2026-09-15",
      "2026-09-16",
      "2026-09-17",
      "2026-09-18",
      "2026-09-19",
      "2026-09-20",
    ]);
    expect(days.map((day) => day.runs)).toEqual([2, 0, 0, 0, 0, 0, 17]);
  });
  it("preserves unknown missing days with partial coverage", () => {
    expect(
      buildRunWeek("2026-09-19T00:00:00Z", [], true).every(
        (day) => day.runs === null,
      ),
    ).toBe(true);
    expect(
      buildRunWeek("2026-09-19T00:00:00Z", [], false).every(
        (day) => day.runs === 0,
      ),
    ).toBe(true);
  });
  it("selects only three valid pending tasks using one-time fallback but never cron execute_at", () => {
    const tasks = [
      task("last"),
      task("cancelled", { status: "cancelled" }),
      task("invalid", { next_run_at: "bad" }),
      task("cron-no-next", { schedule_type: "cron" }),
      task("first", { next_run_at: "2026-09-19T11:00:00Z" }),
      task("second", { next_run_at: "2026-09-19T13:00:00Z" }),
      task("fourth", { execute_at: "2026-09-21T00:00:00Z" }),
    ];
    expect(upcomingSchedules(tasks).map((item) => item.task.task_id)).toEqual([
      "first",
      "second",
      "last",
    ]);
  });
  it("formats a readable date and time in the service timezone", () => {
    expect(scheduleTime("2026-09-20T01:30:00Z", "America/Los_Angeles")).toEqual(
      {
        date: "Sep 19",
        time: "6:30 PM",
      },
    );
  });
  it.each([
    ["starting", null, "unavailable", "Starting"],
    ["ready", true, "unavailable", "Paused"],
    ["ready", false, "idle", "Idle"],
    ["ready", false, "busy", "Working"],
    ["failed", null, "unavailable", "Unavailable"],
  ])("reads useful 503 JSON (%s)", async (phase, paused, status, label) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            runtime_phase: phase,
            admission_paused: paused,
            status,
            active_matrix_operations: 0,
            active_openai_requests: 0,
          }),
          { status: status === "unavailable" ? 503 : 200 },
        ),
      ),
    );
    expect(await fetchRuntimeActivity()).toBe(label);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/responses/activity"),
      expect.anything(),
    );
  });
  it("rejects malformed activity instead of claiming idle", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(new Response(JSON.stringify({ status: "idle" }))),
    );
    await expect(fetchRuntimeActivity()).rejects.toThrow();
  });
});
