import { describe, expect, it } from "vitest";

import {
  getToolOverrides,
  setToolOverridesInEntries,
  toolEntriesForNames,
  toolNames,
  type ToolEntry,
} from "./toolEntry";

describe("toolEntry", () => {
  it("reads tool names from every entry form", () => {
    const rawEntries: ToolEntry[] = [
      "browser",
      { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
      { name: "coding", overrides: { mode: "strict" } },
    ];

    expect(toolNames(rawEntries)).toEqual(["browser", "shell", "coding"]);
  });

  it("reads structured tool overrides", () => {
    const rawEntries: ToolEntry[] = [
      "browser",
      {
        shell: {
          extra_env_passthrough: ["GITEA_TOKEN"],
          shell_path_prepend: ["/run/wrappers/bin"],
        },
      },
    ];

    expect(getToolOverrides("browser", rawEntries)).toBeNull();
    expect(getToolOverrides("shell", rawEntries)).toEqual({
      extra_env_passthrough: ["GITEA_TOKEN"],
      shell_path_prepend: ["/run/wrappers/bin"],
    });
  });

  it("sets structured overrides without dropping unknown keys", () => {
    const rawEntries: ToolEntry[] = [
      {
        shell: {
          extra_env_passthrough: ["GITEA_TOKEN"],
          future_field: "keep-me",
        },
      },
      "browser",
    ];

    expect(
      setToolOverridesInEntries(
        "shell",
        {
          extra_env_passthrough: null,
          shell_path_prepend: ["/run/wrappers/bin"],
        },
        rawEntries,
      ),
    ).toEqual([
      {
        shell: {
          future_field: "keep-me",
          shell_path_prepend: ["/run/wrappers/bin"],
        },
      },
      "browser",
    ]);
  });

  it("keeps override values the patch does not name verbatim", () => {
    const rawEntries: ToolEntry[] = [
      {
        shell: {
          ports: [8080, 8443],
          allowlist: [],
          cleared: null,
          nested: { mode: " strict " },
        },
      },
    ];

    expect(
      setToolOverridesInEntries(
        "shell",
        { extra_env_passthrough: [" TOKEN "] },
        rawEntries,
      ),
    ).toEqual([
      {
        shell: {
          ports: [8080, 8443],
          allowlist: [],
          cleared: null,
          nested: { mode: " strict " },
          extra_env_passthrough: ["TOKEN"],
        },
      },
    ]);
    expect(rawEntries[0]).toEqual({
      shell: {
        ports: [8080, 8443],
        allowlist: [],
        cleared: null,
        nested: { mode: " strict " },
      },
    });
  });

  it("never adds a tool the entries do not list", () => {
    const rawEntries: ToolEntry[] = ["browser"];

    expect(
      setToolOverridesInEntries("shell", { sandbox: "tight" }, rawEntries),
    ).toEqual(["browser"]);
  });

  it("keeps lazy-loading flags from the explicit named form", () => {
    const rawEntries: ToolEntry[] = [
      {
        name: "shell",
        defer: true,
        initial: true,
        overrides: { extra_env_passthrough: ["GITEA_TOKEN"] },
      },
      { name: "browser" },
    ];

    expect(getToolOverrides("shell", rawEntries)).toEqual({
      extra_env_passthrough: ["GITEA_TOKEN"],
      defer: true,
      initial: true,
    });
    expect(
      setToolOverridesInEntries("shell", { sandbox: "tight" }, rawEntries),
    ).toEqual([
      {
        shell: {
          extra_env_passthrough: ["GITEA_TOKEN"],
          defer: true,
          initial: true,
          sandbox: "tight",
        },
      },
      { name: "browser" },
    ]);
  });

  it("removes lazy-loading flags patched to null", () => {
    const rawEntries: ToolEntry[] = [
      { shell: { defer: true, initial: true, future_field: "keep-me" } },
    ];

    expect(
      setToolOverridesInEntries(
        "shell",
        { defer: null, initial: null },
        rawEntries,
      ),
    ).toEqual([{ shell: { future_field: "keep-me" } }]);
    expect(
      setToolOverridesInEntries(
        "shell",
        { defer: null, initial: null, future_field: null },
        rawEntries,
      ),
    ).toEqual(["shell"]);
  });

  it("collapses a structured entry back to a plain string when overrides are cleared", () => {
    const rawEntries: ToolEntry[] = [
      { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
      "browser",
    ];

    expect(
      setToolOverridesInEntries(
        "shell",
        {
          extra_env_passthrough: [],
        },
        rawEntries,
      ),
    ).toEqual(["shell", "browser"]);
  });

  it("keeps the entries of listed tools and adds new names bare", () => {
    const rawEntries: ToolEntry[] = [
      { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
      "browser",
    ];

    expect(
      toolEntriesForNames(["browser", "shell", "coding"], rawEntries),
    ).toEqual([
      "browser",
      { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
      "coding",
    ]);
  });
});
