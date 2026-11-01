import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ScheduleTask } from "@/types/schedule";

import { confirmLeavingUnsavedDrafts } from "@/hooks/useUnsavedDraft";

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
        unreadable_rooms: [],
      }),
    } as Response);
  });

  it("names each room whose schedules could not be read, with the reason", async () => {
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      json: async () => ({
        timezone: "UTC",
        tasks: [visibleTask],
        unreadable_rooms: [
          {
            room_id: "!lobby:example.org",
            room_alias: "#lobby:example.org",
            error: "Unable to retrieve scheduled tasks: M_FORBIDDEN",
          },
        ],
      }),
    } as Response);

    render(<Schedules />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("#lobby:example.org");
    expect(alert).toHaveTextContent(
      "Unable to retrieve scheduled tasks: M_FORBIDDEN",
    );
    expect(screen.getByText("Visible status check")).toBeInTheDocument();
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

  describe("unsaved edits", () => {
    const lobbyTask = {
      ...visibleTask,
      task_id: "lobby-1",
      room_id: "!lobby:example.org",
      room_alias: "#lobby:example.org",
      description: "Lobby greeting",
    } satisfies ScheduleTask;
    const originalInnerWidth = window.innerWidth;

    beforeEach(() => {
      vi.mocked(global.fetch).mockResolvedValue({
        ok: true,
        json: async () => ({
          timezone: "UTC",
          tasks: [silentTask, visibleTask, lobbyTask],
          unreadable_rooms: [],
        }),
      } as Response);
    });

    afterEach(() => {
      vi.restoreAllMocks();
      window.innerWidth = originalInnerWidth;
    });

    async function editQuietTask() {
      render(<Schedules />);
      fireEvent.click(await screen.findByText("Quiet inbox check"));
      const description = screen.getByLabelText("Description");
      fireEvent.change(description, {
        target: { value: "Unsaved description" },
      });
      return description;
    }

    it("asks before selecting another schedule drops the edits", async () => {
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      await editQuietTask();

      fireEvent.click(screen.getByText("Visible status check"));

      expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
      expect(screen.getByLabelText("Description")).toHaveValue(
        "Unsaved description",
      );

      confirmSpy.mockReturnValue(true);
      fireEvent.click(screen.getByText("Visible status check"));

      expect(screen.getByLabelText("Description")).toHaveValue(
        "Visible status check",
      );
    });

    it("does not ask once the edits are undone", async () => {
      const confirmSpy = vi.spyOn(window, "confirm");
      const description = await editQuietTask();
      fireEvent.change(description, {
        target: { value: "Quiet inbox check" },
      });
      const silent = screen.getByRole("checkbox", { name: "Silent delivery" });
      fireEvent.click(silent);
      fireEvent.click(silent);

      expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
      fireEvent.click(screen.getByText("Visible status check"));

      expect(confirmSpy).not.toHaveBeenCalled();
      expect(screen.getByLabelText("Description")).toHaveValue(
        "Visible status check",
      );
    });

    it("counts saved edits as saved even when the list fails to reload", async () => {
      await editQuietTask();
      vi.mocked(global.fetch)
        .mockResolvedValueOnce({ ok: true, json: async () => ({}) } as Response)
        .mockRejectedValueOnce(new Error("list unavailable"));

      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("list unavailable")).toBeInTheDocument();
      expect(confirmLeavingUnsavedDrafts()).toBe(true);
    });

    it("keeps another schedule clean when a save finishes after switching to it", async () => {
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
      await editQuietTask();
      let finishSave: (response: Response) => void = () => {};
      vi.mocked(global.fetch)
        .mockImplementationOnce(
          () =>
            new Promise<Response>((resolve) => {
              finishSave = resolve;
            }),
        )
        .mockRejectedValueOnce(new Error("list unavailable"));

      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      fireEvent.click(screen.getByText("Visible status check"));
      expect(screen.getByLabelText("Description")).toHaveValue(
        "Visible status check",
      );
      finishSave({ ok: true, json: async () => ({}) } as Response);

      expect(await screen.findByText("list unavailable")).toBeInTheDocument();
      confirmSpy.mockReturnValue(false);
      expect(confirmLeavingUnsavedDrafts()).toBe(true);
    });

    it("does not ask when the selected schedule is selected again", async () => {
      const confirmSpy = vi.spyOn(window, "confirm");
      await editQuietTask();

      fireEvent.click(screen.getByText("Quiet inbox check"));

      expect(confirmSpy).not.toHaveBeenCalled();
      expect(screen.getByLabelText("Description")).toHaveValue(
        "Unsaved description",
      );
    });

    it("asks before the back button or a swipe back drops the edits", async () => {
      window.innerWidth = 400;
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      await editQuietTask();

      fireEvent.click(screen.getByRole("button", { name: "Back" }));
      fireEvent.touchStart(document, {
        touches: [{ clientX: 10, clientY: 100 }],
      });
      fireEvent.touchEnd(document, {
        changedTouches: [{ clientX: 200, clientY: 100 }],
      });

      expect(confirmSpy).toHaveBeenCalledTimes(2);
      expect(screen.getByLabelText("Description")).toHaveValue(
        "Unsaved description",
      );
    });

    it("asks before a refresh drops the edits", async () => {
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      await editQuietTask();

      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

      expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(screen.getByLabelText("Description")).toHaveValue(
        "Unsaved description",
      );
    });

    it("asks before a room filter that hides the schedule drops the edits", async () => {
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      const pickRoomFilter = async (name: string) => {
        fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name }));
      };
      await editQuietTask();

      // Neither the schedule's own room nor All rooms hides it.
      await pickRoomFilter("#ops:example.org");
      await pickRoomFilter("All rooms");
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(screen.queryByText(/^Room: /)).not.toBeInTheDocument();

      await pickRoomFilter("#ops:example.org");
      await pickRoomFilter("#lobby:example.org");

      expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
      expect(screen.getByLabelText("Description")).toHaveValue(
        "Unsaved description",
      );
      expect(screen.getByText("Room: #ops:example.org")).toBeInTheDocument();
    });
  });
});
