import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useConfigStore } from "@/store/configStore";

import App, { shouldShowBlockingDiagnosticOverlay } from "./App";

vi.mock("@/store/configStore", () => ({
  useConfigStore: vi.fn(),
}));

vi.mock("@/components/Dashboard/Dashboard", () => ({
  Dashboard: () => <p>Dashboard page</p>,
}));

vi.mock("@/components/Credentials/Credentials", () => ({
  Credentials: () => <p>Credentials page</p>,
}));

// A page whose unsaved draft lives in its own state, as the Skills editor's does.
vi.mock("@/components/Skills/Skills", async () => {
  const { useState } = await import("react");
  const { useUnsavedDraft } = await import("@/hooks/useUnsavedDraft");
  return {
    Skills: function Skills() {
      const [draft, setDraft] = useState("");
      useUnsavedDraft(draft !== "");
      return (
        <textarea
          aria-label="Skill draft"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
      );
    },
  };
});

function mockBrowserStorageAndMedia() {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: vi.fn(() => "system"),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
    },
  });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    }),
  });
}

describe("shouldShowBlockingDiagnosticOverlay", () => {
  it("keeps access overlays blocking for auth failures", () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: "global",
          message:
            "Authentication required. Please log in to access this instance.",
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: false },
      ),
    ).toBe(true);
  });

  it("keeps the dashboard visible when a draft or validation details make recovery possible", () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: "global",
          message: "Network error",
          blocking: true,
        },
        { hasLoadedConfig: true, hasRecoveryConfig: false },
      ),
    ).toBe(false);
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: true,
        },
        { hasLoadedConfig: true, hasRecoveryConfig: false },
      ),
    ).toBe(false);
  });

  it("keeps recovery mode visible for generic blocking failures when a recovery draft exists", () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: "global",
          message: "Server error. Please try again later or contact support.",
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: true },
      ),
    ).toBe(true);
  });

  it("still blocks generic failures when there is no recoverable config state", () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: "global",
          message: "Server error. Please try again later or contact support.",
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: false },
      ),
    ).toBe(true);
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: false },
      ),
    ).toBe(true);
  });
});

describe("App recovery mode", () => {
  const mockSaveRecoveryConfigSource = vi.fn();

  beforeEach(() => {
    mockSaveRecoveryConfigSource.mockReset();
    mockBrowserStorageAndMedia();
    vi.mocked(useConfigStore).mockReturnValue({
      loadConfig: vi.fn(),
      config: null,
      recoveryConfigSource: "agents:\n  broken: true\n",
      recoveryConfigSourceOriginal: "agents:\n  broken: false\n",
      updateRecoveryConfigSource: vi.fn(),
      saveRecoveryConfigSource: mockSaveRecoveryConfigSource,
      syncStatus: "error",
      diagnostics: [
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: true,
        },
      ],
      isLoading: false,
      selectedAgentId: null,
      selectedTeamId: null,
      selectedRoomId: null,
    } as never);
  });

  it("renders the recovery editor when a blocking recovery draft exists", () => {
    render(<App />);

    expect(
      screen.getByRole("button", { name: "Save Replacement Config" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/could not be loaded\. edit the raw configuration/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue(
      "agents:\n  broken: true\n",
    );
  });

  it("surfaces stale recovery save results", async () => {
    mockSaveRecoveryConfigSource.mockResolvedValue({ status: "stale" });

    render(<App />);

    fireEvent.click(
      screen.getByRole("button", { name: "Save Replacement Config" }),
    );

    await waitFor(() => {
      expect(
        screen.getByText("Save was superseded by newer recovery edits."),
      ).toBeInTheDocument();
    });
  });

  it("shows conflict recovery guidance alongside existing validation issues", () => {
    const state = useConfigStore();
    state.diagnostics = [
      {
        kind: "global",
        message:
          "Configuration changed elsewhere. Copy your changes, then refresh this page.",
        blocking: true,
      },
      {
        kind: "global",
        message: "Configuration validation failed",
        blocking: true,
      },
      {
        kind: "validation",
        issue: {
          loc: ["agents", "helper", "role"],
          msg: "role is required",
          type: "value_error",
        },
      },
    ];
    render(<App />);

    expect(
      screen.getByText(/Configuration changed elsewhere/),
    ).toBeInTheDocument();
    expect(screen.getByText(/role is required/)).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue(state.recoveryConfigSource);
  });

  it("shows authentication recovery even when an earlier conflict remains", () => {
    useConfigStore().diagnostics = [
      {
        kind: "global",
        code: "config_conflict",
        message: "Configuration changed elsewhere.",
        blocking: true,
      },
      {
        kind: "global",
        message:
          "Authentication required. Please log in to access this instance.",
        blocking: true,
      },
    ];
    render(<App />);

    expect(
      screen.getByRole("heading", { name: "Access Required" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});

describe("App tab switching", () => {
  beforeEach(() => {
    mockBrowserStorageAndMedia();
    vi.mocked(useConfigStore).mockReturnValue({
      loadConfig: vi.fn(),
      config: null,
      recoveryConfigSource: null,
      recoveryConfigSourceOriginal: null,
      syncStatus: "synced",
      diagnostics: [],
      isLoading: false,
      isDirty: false,
      selectedAgentId: null,
      selectedTeamId: null,
      selectedRoomId: null,
    } as never);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function typeSkillDraft() {
    render(<App />);
    fireEvent.click(screen.getByRole("link", { name: "Skills" }));
    const draft = await screen.findByLabelText("Skill draft");
    fireEvent.change(draft, { target: { value: "unsaved edit" } });
  }

  it("asks before leaving a tab with an unsaved draft, and Cancel keeps it", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await typeSkillDraft();

    fireEvent.click(screen.getByRole("link", { name: "Credentials" }));

    await waitFor(() =>
      expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?"),
    );
    expect(screen.getByLabelText("Skill draft")).toHaveValue("unsaved edit");
    expect(screen.queryByText("Credentials page")).not.toBeInTheDocument();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(screen.getByRole("link", { name: "Credentials" }));

    expect(await screen.findByText("Credentials page")).toBeInTheDocument();
  });

  it("asks before the browser back button leaves a tab with an unsaved draft", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<App />);
    fireEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    await screen.findByText("Dashboard page");
    fireEvent.click(screen.getByRole("link", { name: "Skills" }));
    const draft = await screen.findByLabelText("Skill draft");
    fireEvent.change(draft, { target: { value: "unsaved edit" } });

    window.history.back();

    await waitFor(() =>
      expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?"),
    );
    expect(screen.getByLabelText("Skill draft")).toHaveValue("unsaved edit");
    expect(screen.queryByText("Dashboard page")).not.toBeInTheDocument();
  });

  it("makes reloading or closing the tab ask while the configuration has unsaved changes", () => {
    // True when the browser would show its leave-page prompt.
    const unloadAsks = () =>
      !window.dispatchEvent(new Event("beforeunload", { cancelable: true }));
    const { unmount } = render(<App />);
    expect(unloadAsks()).toBe(false);
    unmount();

    vi.mocked(useConfigStore).mockReturnValue({
      ...vi.mocked(useConfigStore)(),
      isDirty: true,
    });
    render(<App />);
    expect(unloadAsks()).toBe(true);
  });

  it("moves focus with Skip to content without a history entry the router cannot block", () => {
    render(<App />);

    // A browser follows a native fragment link with a history entry outside
    // the router, and the router cannot hold back a back press into it.
    const followed = fireEvent.click(
      screen.getByRole("link", { name: "Skip to content" }),
    );

    expect(followed).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole("main"));
  });

  it("does not ask without an unsaved draft or when staying on the tab", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    await typeSkillDraft();

    fireEvent.click(screen.getByRole("link", { name: "Skills" }));
    fireEvent.change(screen.getByLabelText("Skill draft"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("link", { name: "Credentials" }));

    expect(await screen.findByText("Credentials page")).toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});
