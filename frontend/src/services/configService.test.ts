import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Config } from "@/types/config";
import {
  ConfigStaleError,
  saveConfig,
  saveRawConfigSource,
} from "./configService";

describe.each([
  {
    name: "saveConfig",
    save: () => saveConfig({} as Config, 3),
    fallback: "Failed to save configuration",
  },
  {
    name: "saveRawConfigSource",
    save: () => saveRawConfigSource("agents: {}\n", 3),
    fallback: "Failed to save raw configuration",
  },
])("$name errors", ({ save, fallback }) => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([
    [409, "Edited elsewhere", new ConfigStaleError("Edited elsewhere")],
    [409, "", new ConfigStaleError()],
    [500, "Disk full", new Error("Disk full")],
    [500, "", new Error(`${fallback} (Error 500)`)],
  ])("reports a %i response with detail %j", async (status, detail, error) => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail }), { status }),
    );

    const rejection = save();
    await expect(rejection).rejects.toThrow(error);
    await expect(rejection).rejects.toBeInstanceOf(error.constructor);
  });
});
