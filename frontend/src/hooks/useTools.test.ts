import { describe, expect, it } from "vitest";
import { mapToolToIntegration, type ToolInfo } from "./useTools";

const tool = (setup_type: string, status: string): ToolInfo => ({
  name: "example",
  display_name: "Example",
  description: "An example tool",
  category: "information",
  status,
  setup_type,
  icon: null,
  icon_color: null,
  config_fields: null,
  dependencies: null,
});

describe("mapToolToIntegration", () => {
  it.each([
    ["api_key", "available", "api_key", "connected"],
    ["oauth", "available", "oauth", "connected"],
    ["special", "available", "special", "connected"],
    ["none", "available", "none", "available"],
    ["unknown", "available", "none", "available"],
    ["api_key", "requires_config", "api_key", "not_connected"],
    ["none", "requires_config", "none", "not_connected"],
    ["oauth", "unavailable", "oauth", "available"],
  ])(
    "maps a %s tool with status %s to setup %s and status %s",
    (setupType, status, expectedSetupType, expectedStatus) => {
      expect(mapToolToIntegration(tool(setupType, status))).toMatchObject({
        setup_type: expectedSetupType,
        status: expectedStatus,
      });
    },
  );
});
