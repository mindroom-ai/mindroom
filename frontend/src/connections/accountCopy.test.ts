import { describe, expect, it } from "vitest";
import {
  administratorLimitNote,
  deleteServiceWarning,
  disconnectDescription,
  formatList,
  personalAccountRefusal,
  personalSandboxRefusal,
  passThroughCaption,
  personalScopeLine,
  removeLimitWarning,
  sharedAccountBody,
  sharedAccountStatus,
  sharedAgentNote,
} from "./accountCopy";

describe("account copy", () => {
  it("lists one, two, and three names", () => {
    expect(formatList(["@bob:x"])).toBe("@bob:x");
    expect(formatList(["@bob:x", "@carol:x"])).toBe("@bob:x and @carol:x");
    expect(formatList(["a", "b", "c"])).toBe("a, b, and c");
  });

  it("asks about a shared account in the spec's words", () => {
    expect(sharedAccountBody("Shared Dev", "Google")).toBe(
      "Everyone who uses Shared Dev will act as the Google account you pick. Use a team account, not your personal one.",
    );
  });

  it("says who a connected account is shared with", () => {
    expect(sharedAccountStatus("team@example.org", "Shared Dev")).toBe(
      "Connected as team@example.org · shared with everyone using Shared Dev",
    );
    expect(sharedAccountStatus(null, "Shared Dev")).toBe(
      "Connected · shared with everyone using Shared Dev",
    );
  });

  it("describes a disconnect with the services that stop working", () => {
    expect(
      disconnectDescription("GitHub", "Personal Mind", [
        "GitHub",
        "GitHub (a/b)",
      ]),
    ).toBe(
      "This removes the saved GitHub connection for Personal Mind. These services stop working until an account is connected again: GitHub and GitHub (a/b).",
    );
    expect(disconnectDescription("GitHub", "Personal Mind", [])).toBe(
      "This removes the saved GitHub connection for Personal Mind.",
    );
  });

  it("tells managers and others who can change the keys of a shared agent", () => {
    expect(sharedAgentNote(true, ["@bob:x"])).toBe(
      "You manage this shared agent's keys. Everyone who uses it shares them.",
    );
    expect(sharedAgentNote(false, ["@bob:x", "@carol:x"])).toBe(
      "Only @bob:x and @carol:x can change keys for this shared agent.",
    );
    expect(sharedAgentNote(false, [])).toBe(
      "Only administrators can change keys for this shared agent.",
    );
  });

  it("speaks of a shared agent's accounts instead of keys where no keys are shown", () => {
    expect(sharedAgentNote(false, ["@bob:x", "@carol:x"], false)).toBe(
      "Only @bob:x and @carol:x can change this shared agent's accounts.",
    );
    expect(sharedAgentNote(false, [], false)).toBe(
      "Only administrators can change this shared agent's accounts.",
    );
  });

  it("gives managers and others different advice for a refused personal account", () => {
    expect(personalAccountRefusal("GitHub", true, ["@bob:x"])).toBe(
      "Personal GitHub accounts can't be used by shared agents. Add a team API key instead.",
    );
    expect(personalAccountRefusal("GitHub", false, ["@bob:x"])).toBe(
      "Personal GitHub accounts can't be used by shared agents. Ask @bob:x to add a team API key.",
    );
    expect(personalAccountRefusal("GitHub", false, [])).toBe(
      "Personal GitHub accounts can't be used by shared agents. Ask an administrator to add a team API key.",
    );
  });

  it("tells a personal agent's user the sandbox is shared, not the agent", () => {
    expect(personalSandboxRefusal("GitHub")).toBe(
      "Personal GitHub accounts can't be used in this sandbox, which other users' commands share. Add an API key instead.",
    );
  });

  it("says who loses access when a service is deleted, by scope", () => {
    expect(deleteServiceWarning("user_agent", "Coder", [])).toBe(
      "This deletes the service and its saved key. Your Coder loses access through it.",
    );
    expect(
      deleteServiceWarning("user", "Coder", ["Coder", "Writer", "Scripter"]),
    ).toBe(
      "This deletes the service and its saved key. All your agents that share your personal workspace lose access through it: Coder, Writer, Scripter.",
    );
    expect(deleteServiceWarning("shared", "Team Bot", [])).toBe(
      "This deletes the service and its shared key. Everyone using Team Bot loses access through it.",
    );
    expect(deleteServiceWarning("unscoped", "Team Bot", [])).toBe(
      "This deletes the service and its shared key. Every agent without a scope loses access through it.",
    );
  });

  it("tells the viewer who else can see the requests, and whose requests are shown", () => {
    expect(passThroughCaption(false, false)).toBe(
      "Requests to sites without a service pass straight through; MindRoom sees only the host. Administrators can also see these requests (method, host, and path). Only your own requests are shown.",
    );
    // A shared or unscoped agent's credential managers see everyone's requests through it.
    expect(passThroughCaption(true, false)).toBe(
      "Requests to sites without a service pass straight through; MindRoom sees only the host. Administrators and this agent's credential managers can also see these requests (method, host, and path). Only your own requests are shown.",
    );
    expect(passThroughCaption(true, true)).toBe(
      "Requests to sites without a service pass straight through; MindRoom sees only the host. Administrators and this agent's credential managers can also see these requests (method, host, and path). Requests from everyone who uses this agent are shown.",
    );
  });

  it("says who uses a personal agent's keys, and nothing for a shared one", () => {
    expect(personalScopeLine("user_agent")).toBe(
      "Only you use this agent's keys.",
    );
    expect(personalScopeLine("user")).toBe(
      "Shared by all your agents in your personal workspace.",
    );
    expect(personalScopeLine("shared")).toBeNull();
    expect(personalScopeLine("unscoped")).toBeNull();
  });

  it("says for whom a restriction also limits the administrator's services", () => {
    expect(administratorLimitNote("user_agent")).toBe(
      "This also limits services from your administrator on these hosts, for this agent only.",
    );
    expect(administratorLimitNote("user")).toBe(
      "This also limits services from your administrator on these hosts, for all your agents that share your personal workspace.",
    );
    expect(administratorLimitNote("shared")).toBe(
      "This also limits services from your administrator on these hosts, for everyone using this agent.",
    );
    expect(administratorLimitNote("unscoped")).toBe(
      "This also limits services from your administrator on these hosts, for every agent without a scope.",
    );
  });

  it("says what removing a limit gives back, to whom", () => {
    expect(removeLimitWarning("user_agent", "Mind", [])).toBe(
      "This removes the limit. Your Mind can then reach every repository the key or connected account allows.",
    );
    expect(removeLimitWarning("user", "Mind", ["Mind", "Notes"])).toBe(
      "This removes the limit. All your agents that share your personal workspace (Mind, Notes) can then reach every repository the key or connected account allows.",
    );
    expect(removeLimitWarning("shared", "Team Bot", [])).toBe(
      "This removes the limit. Everyone using Team Bot can then reach every repository the key or connected account allows.",
    );
    expect(removeLimitWarning("unscoped", "Team Bot", [])).toBe(
      "This removes the limit. Every agent without a scope can then reach every repository the key or connected account allows.",
    );
  });
});
