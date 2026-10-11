import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ScheduleTask } from "@/types/schedule";

import { Schedules } from "./Schedules";

const silentTask = {
  task_id: "quiet-1",
  room_id: "!ops:example.org",
  room_alias: "#ops:example.org",
  status: "pending",
  schedule_type: "cron",
  execute_at: null,
  next_run_at: "2026-08-24T16:00:00Z",
  cron_expression: "0 9 * * *",
  cron_description: "Every day at 9:00 AM",
  description: "Quiet inbox check",
  message: "Check the inbox for urgent mail",
  history_limit: 0,
  thread_id: null,
  new_thread: false,
  silent: true,
  created_by: "@owner:example.org",
  created_at: "2026-08-23T10:00:00Z",
} satisfies ScheduleTask;

const visibleTask = {
  ...silentTask,
  task_id: "visible-1",
  description: "Visible status check",
  message: "Post the current deployment status",
  silent: false,
} satisfies ScheduleTask;

describe("Schedules", () => {
  beforeEach(() => {
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      json: async () => ({
        timezone: "UTC",
        tasks: [silentTask, visibleTask],
      }),
    } as Response);
  });

  it("identifies visible schedules and exposes their delivery mode", async () => {
    render(<Schedules />);

    fireEvent.click(await screen.findByText("Visible status check"));

    expect(screen.getByText("Visible")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Silent delivery" }),
    ).not.toBeChecked();
  });

  it("identifies silent schedules and exposes their delivery mode", async () => {
    render(<Schedules />);

    fireEvent.click(await screen.findByText("Quiet inbox check"));

    expect(screen.getByText("Silent")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Silent delivery" }),
    ).toBeChecked();
  });

  it("sends a changed delivery mode to the schedules API", async () => {
    render(<Schedules />);

    fireEvent.click(await screen.findByText("Quiet inbox check"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Silent delivery" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(3));
    const [, updateRequest] = vi.mocked(global.fetch).mock.calls[1];
    expect(JSON.parse(String(updateRequest?.body))).toEqual({
      room_id: "!ops:example.org",
      message: "Check the inbox for urgent mail",
      description: "Quiet inbox check",
      schedule_type: "cron",
      cron_expression: "0 9 * * *",
      silent: false,
    });
  });

  it("preserves silent delivery when editing another field", async () => {
    render(<Schedules />);

    fireEvent.click(await screen.findByText("Quiet inbox check"));
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Updated quiet inbox check" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(3));
    const [, updateRequest] = vi.mocked(global.fetch).mock.calls[1];
    expect(JSON.parse(String(updateRequest?.body))).toMatchObject({
      description: "Updated quiet inbox check",
      silent: true,
    });
  });
});
