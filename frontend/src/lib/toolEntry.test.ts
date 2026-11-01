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
    const entries: ToolEntry[] = [
      "browser",
      { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
    ];

    expect(toolNames(entries)).toEqual(["browser", "shell"]);
  });

  it("reads structured tool overrides", () => {
    const entries: ToolEntry[] = [
      "browser",
      {
        shell: {
          extra_env_passthrough: ["GITEA_TOKEN"],
          shell_path_prepend: ["/run/wrappers/bin"],
        },
      },
    ];

    expect(getToolOverrides("browser", entries)).toBeNull();
    expect(getToolOverrides("shell", entries)).toEqual({
      extra_env_passthrough: ["GITEA_TOKEN"],
      shell_path_prepend: ["/run/wrappers/bin"],
    });
  });

  it("sets structured overrides without dropping unknown keys", () => {
    const entries: ToolEntry[] = [
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
        entries,
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
    const entries: ToolEntry[] = [
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
        { extra_env_passthrough: ["TOKEN"] },
        entries,
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
    expect(entries[0]).toEqual({
      shell: {
        ports: [8080, 8443],
        allowlist: [],
        cleared: null,
        nested: { mode: " strict " },
      },
    });
  });

  it("stores an empty list, which differs from a missing key", () => {
    expect(
      setToolOverridesInEntries("docker", { include_tools: [] }, [
        { docker: { include_tools: ["list_containers"] } },
      ]),
    ).toEqual([{ docker: { include_tools: [] } }]);
  });

  it("never adds a tool the entries do not list", () => {
    const entries: ToolEntry[] = ["browser"];

    expect(
      setToolOverridesInEntries("shell", { sandbox: "tight" }, entries),
    ).toEqual(["browser"]);
  });

  it("removes lazy-loading flags patched to null", () => {
    const entries: ToolEntry[] = [
      { shell: { defer: true, initial: true, future_field: "keep-me" } },
    ];

    expect(
      setToolOverridesInEntries(
        "shell",
        { defer: null, initial: null },
        entries,
      ),
    ).toEqual([{ shell: { future_field: "keep-me" } }]);
  });

  it("collapses a structured entry back to a plain string when overrides are cleared", () => {
    expect(
      setToolOverridesInEntries(
        "shell",
        { defer: null, initial: null, future_field: null },
        [{ shell: { defer: true, initial: true, future_field: "keep-me" } }],
      ),
    ).toEqual(["shell"]);
  });

  it("reads overrides that are not a mapping as none", () => {
    const entries = [{ shell: ["sandbox"] }] as unknown as ToolEntry[];

    expect(getToolOverrides("shell", entries)).toBeNull();
  });

  it("keeps the entries of listed tools and adds new names bare", () => {
    const entries: ToolEntry[] = [
      { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
      "browser",
    ];

    expect(
      toolEntriesForNames(["browser", "shell", "coding"], entries),
    ).toEqual([
      "browser",
      { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
      "coding",
    ]);
  });
});
