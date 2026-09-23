import { describe, expect, it } from "vitest";

import type { JsonSchema } from "@/lib/configSchema";

import { resolveSettingsSections, sectionHasIssue } from "./settingsSections";

const objectOf = (...keys: string[]): JsonSchema => ({
  type: "object",
  properties: Object.fromEntries(keys.map((key) => [key, { type: "string" }])),
});

const ROOT: JsonSchema = {
  type: "object",
  properties: {
    agents: { type: "object", additionalProperties: { type: "object" } },
    defaults: { $ref: "#/$defs/DefaultsConfig" },
    router: { $ref: "#/$defs/RouterConfig" },
    room_defaults: { $ref: "#/$defs/RoomDefaultsConfig" },
    timezone: { type: "string", default: "UTC" },
    brand_new_root: { type: "boolean", default: false },
  },
  $defs: {
    DefaultsConfig: objectOf("markdown", "tools", "brand_new_default"),
    RouterConfig: objectOf("model"),
    RoomDefaultsConfig: objectOf("admins", "encrypted", "brand_new_room_key"),
  },
};

describe("resolveSettingsSections", () => {
  const sections = resolveSettingsSections(ROOT);
  const byId = Object.fromEntries(
    sections.map((section) => [section.id, section]),
  );

  it("keeps listed keys that the schema defines, in listed order", () => {
    expect(byId.responses.entries).toEqual([
      { root: "defaults", keys: ["markdown"] },
    ]);
    expect(byId.tools.entries).toEqual([{ root: "defaults", keys: ["tools"] }]);
    expect(byId.router.entries).toEqual([{ root: "router" }]);
  });

  it("drops sections whose roots the schema lacks", () => {
    expect(byId["personal-rooms"]).toBeUndefined();
    expect(byId.diagnostics).toBeUndefined();
  });

  it("collects unclaimed roots and keys in Other", () => {
    expect(byId.other.entries).toEqual([
      { root: "defaults", keys: ["brand_new_default"] },
      { root: "room_defaults", keys: ["brand_new_room_key"] },
      { root: "brand_new_root" },
    ]);
  });

  it("leaves page-owned roots and keys to their pages", () => {
    const rendered = sections.flatMap((section) => section.entries);
    expect(rendered.some((entry) => entry.root === "agents")).toBe(false);
    expect(
      rendered.some((entry) => entry.keys?.includes("admins") ?? false),
    ).toBe(false);
  });

  it("finds issues inside the keys or roots a section renders", () => {
    const issue = (...loc: Array<string | number>) => ({
      loc,
      msg: "bad",
      type: "value_error",
    });
    expect(
      sectionHasIssue(byId.responses, [issue("defaults", "markdown")]),
    ).toBe(true);
    expect(sectionHasIssue(byId.responses, [issue("defaults", "tools")])).toBe(
      false,
    );
    // Errors on a partly rendered root itself appear in each of its sections.
    expect(sectionHasIssue(byId.tools, [issue("defaults")])).toBe(true);
    expect(sectionHasIssue(byId.router, [issue("router", "model", 0)])).toBe(
      true,
    );
  });
});
