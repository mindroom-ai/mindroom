import { describe, expect, it } from "vitest";
import { formFromService, type EgressServiceForm } from "./egressServiceForm";
import {
  configPlaceholderOwners,
  configPlaceholderWarning,
  overlapWarnings,
  presetPlaceholderWarnings,
  scopeNote,
} from "./egressServiceNotes";
import { bearerRule, presets } from "@/test/egressServiceTestKit";
import type { AuthoredEgressService } from "./types";

describe("a placeholder the administrator also sets", () => {
  const config = { GH_TOKEN: "GitHub" };

  it("warns with the service's display name", () => {
    expect(configPlaceholderWarning("GH_TOKEN", config)).toBe(
      "GH_TOKEN is also set by GitHub from your administrator; when both are set, theirs is used.",
    );
    expect(configPlaceholderWarning(" GH_TOKEN ", config)).toContain(
      "GH_TOKEN is also set by GitHub",
    );
  });

  it("stays quiet for any other name, including inherited object keys", () => {
    expect(configPlaceholderWarning("MY_API_KEY", config)).toBeNull();
    expect(configPlaceholderWarning("", config)).toBeNull();
    expect(configPlaceholderWarning("constructor", config)).toBeNull();
  });
});

describe("finding who sets a placeholder in a listing", () => {
  const listed = (
    source: "config" | "user",
    display_name: string,
    placeholder_names?: string[],
  ) => ({ source, display_name, placeholder_names });

  it("maps each variable to the administrator's service that sets it", () => {
    expect(
      configPlaceholderOwners([
        listed("config", "GitHub", ["GH_TOKEN", "GITHUB_TOKEN"]),
        listed("config", "Other", ["GH_TOKEN", "OTHER_KEY"]),
      ]),
    ).toEqual({
      GH_TOKEN: "GitHub",
      GITHUB_TOKEN: "GitHub",
      OTHER_KEY: "Other",
    });
  });

  it("ignores the user's own services and services that list none", () => {
    expect(
      configPlaceholderOwners([
        listed("user", "Mine", ["MY_KEY"]),
        listed("config", "Plain"),
      ]),
    ).toEqual({});
  });

  it("keeps a variable named like an inherited object key", () => {
    expect(
      configPlaceholderOwners([listed("config", "Odd", ["constructor"])]),
    ).toEqual({
      constructor: "Odd",
    });
  });
});

describe("the note on whose service this is", () => {
  it("names the agent that owns a user_agent service", () => {
    expect(scopeNote("user_agent", "Code", ["Code"])).toBe(
      "Only your Code uses this service and its key.",
    );
  });

  it("lists the agents that share a user's workspace", () => {
    expect(scopeNote("user", "Code", ["Code", "Research", "Notes"])).toBe(
      "All your agents that share your personal workspace use this service and its key: Code, Research, Notes.",
    );
  });

  it("says nothing for shared, unscoped, or unknown scopes", () => {
    expect(scopeNote("shared", "Team", [])).toBeNull();
    expect(scopeNote("unscoped", "Team", [])).toBeNull();
    expect(scopeNote(undefined, "Team", [])).toBeNull();
  });
});

describe("a placeholder the preset sets that an administrator's service also sets", () => {
  const github = presets.find((preset) => preset.id === "github");
  const config = { GH_TOKEN: "GitHub", GITHUB_TOKEN: "Work GitHub" };
  const warnings = (
    changes: Partial<EgressServiceForm>,
    owners: Record<string, string> = config,
  ) =>
    presetPlaceholderWarnings(
      { ...formFromService("svc", { preset: "github" }), ...changes },
      github,
      owners,
    );

  it("warns for each variable the preset sets, in the preset's order", () => {
    expect(warnings({})).toEqual([
      "GH_TOKEN is also set by GitHub from your administrator; when both are set, theirs is used.",
      "GITHUB_TOKEN is also set by Work GitHub from your administrator; when both are set, theirs is used.",
    ]);
    expect(warnings({}, { GITHUB_TOKEN: "GitHub" })).toHaveLength(1);
    expect(warnings({}, {})).toEqual([]);
  });

  it("says nothing once the service lists its own placeholders or drops the preset's", () => {
    expect(
      warnings({ placeholders: [{ name: "MY_API_KEY", value: "x" }] }),
    ).toEqual([]);
    expect(
      warnings({
        explicitlyEmpty: {
          ...formFromService("svc", null).explicitlyEmpty,
          placeholders: true,
        },
      }),
    ).toEqual([]);
  });

  it("says nothing without a preset or while the presets load", () => {
    const custom = formFromService("svc", null);
    expect(presetPlaceholderWarnings(custom, github, config)).toEqual([]);
    expect(
      presetPlaceholderWarnings(
        formFromService("svc", { preset: "github" }),
        undefined,
        config,
      ),
    ).toEqual([]);
  });
});

describe("services of the agent that already send a key to a website", () => {
  const rule = (host: string, auth_type: string | null = "bearer") => ({
    host,
    port: null,
    path_prefix: "/",
    auth_type: auth_type as "bearer" | null,
    auth_name: null,
  });
  const listed = (
    name: string,
    display_name: string,
    rules: ReturnType<typeof rule>[],
    restrict_only = false,
  ) => ({ name, display_name, rules, restrict_only });
  const form = (service: AuthoredEgressService | null = null) =>
    formFromService("svc", service ?? { rules: [bearerRule] });

  it("names each such service once with the websites they share", () => {
    expect(
      overlapWarnings(
        formFromService("svc", {
          rules: [
            { host: "API.github.com", auth: { type: "bearer" } },
            { host: "github.com", auth: { type: "basic", username: "x" } },
          ],
        }),
        undefined,
        [
          listed("github", "GitHub", [
            rule("api.github.com"),
            rule("github.com", "basic"),
            rule("uploads.github.com"),
          ]),
          listed("other", "Other", [rule("other.example.com")]),
        ],
        undefined,
      ),
    ).toEqual([
      "GitHub already sends a key to api.github.com and github.com for this agent. An exact host wins, then a specific port, then the longest path, then your administrator's services before your own, then the service name alphabetically.",
    ]);
  });

  it("reads a preset's websites when the form uses the preset's rules", () => {
    const github = presets.find((preset) => preset.id === "github");
    expect(
      overlapWarnings(
        formFromService("svc", { preset: "github" }),
        github,
        [listed("mine", "Mine", [rule("uploads.github.com")])],
        undefined,
      ),
    ).toEqual([
      "Mine already sends a key to uploads.github.com for this agent. An exact host wins, then a specific port, then the longest path, then your administrator's services before your own, then the service name alphabetically.",
    ]);
    expect(
      overlapWarnings(
        formFromService("svc", { preset: "github" }),
        undefined,
        [listed("mine", "Mine", [rule("uploads.github.com")])],
        undefined,
      ),
    ).toEqual([]);
  });

  it("skips a service that only limits paths, one that sends no key, the one being edited, and an empty host", () => {
    const others = [
      listed("limit", "Limit", [rule("api.example.com")], true),
      listed("nokey", "NoKey", [rule("api.example.com", null)]),
      listed("self", "Self", [rule("api.example.com")]),
    ];
    expect(overlapWarnings(form(), undefined, others, "self")).toEqual([]);
    expect(
      overlapWarnings(
        formFromService("svc", {
          rules: [{ host: " ", auth: { type: "bearer" } }],
        }),
        undefined,
        [listed("a", "A", [rule("")])],
        undefined,
      ),
    ).toEqual([]);
  });
});
