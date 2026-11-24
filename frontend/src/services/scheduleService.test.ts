import { afterEach, describe, expect, it, vi } from "vitest";
import { updateSchedule } from "./scheduleService";

describe("scheduleService", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([
    {
      body: JSON.stringify({ detail: "Schedule not found" }),
      message: "Schedule not found",
    },
    {
      body: JSON.stringify({ detail: "" }),
      message: "Request failed (Error 404)",
    },
    { body: "<html>Not found</html>", message: "Request failed (Error 404)" },
  ])(
    "reports a failed schedule update as $message",
    async ({ body, message }) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response(body, { status: 404 })),
      );

      await expect(
        updateSchedule("task-1", { room_id: "!room:example.org" }),
      ).rejects.toThrow(message);
    },
  );
});
