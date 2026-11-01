import { describe, expect, it } from "vitest";
import {
  getGlobalConfigDiagnostics,
  type ConfigDiagnostic,
} from "./configValidation";

const diagnostics: ConfigDiagnostic[] = [
  {
    kind: "global",
    message: "Configuration validation failed",
    blocking: false,
  },
  {
    kind: "validation",
    issue: {
      loc: ["config"],
      msg: "At least one model must be configured.",
      type: "value_error",
    },
  },
  {
    kind: "validation",
    issue: {
      loc: ["agents", "mind", "role"],
      msg: "Role must not be blank.",
      type: "value_error",
    },
  },
];

describe("getGlobalConfigDiagnostics", () => {
  it("leaves validation issues, whole-config ones included, to the located issue list", () => {
    expect(getGlobalConfigDiagnostics(diagnostics)).toEqual([
      {
        kind: "global",
        message: "Configuration validation failed",
        blocking: false,
      },
    ]);
  });
});
