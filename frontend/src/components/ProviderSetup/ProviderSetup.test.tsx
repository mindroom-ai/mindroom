import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ProviderSetupBanner,
  defaultProviderChoice,
  providerSetupMessage,
  remainingProviderHint,
  type MissingProviderKey,
} from "./ProviderSetup";

const mockToast = vi.fn();
vi.mock("@/components/ui/toaster", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

const OPENROUTER_MISSING: MissingProviderKey[] = [
  { provider: "openrouter", models: ["default"] },
];

type FetchCall = [string, RequestInit | undefined];

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Bad Request",
    json: async () => body,
  };
}

function mockFetch(
  handler: (url: string, init?: RequestInit) => ReturnType<typeof jsonResponse>,
) {
  global.fetch = vi.fn(async (url: string, init?: RequestInit) =>
    handler(url, init),
  ) as unknown as typeof fetch;
}

function fetchCalls(): FetchCall[] {
  return (global.fetch as unknown as { mock: { calls: FetchCall[] } }).mock
    .calls;
}

function renderBanner() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ProviderSetupBanner refreshKey="1:dashboard" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("provider setup helpers", () => {
  it("names the providers the agents are missing", () => {
    expect(providerSetupMessage(OPENROUTER_MISSING)).toBe(
      "Your agents can't reply until MindRoom has an API key for OpenRouter.",
    );
    expect(
      providerSetupMessage([
        { provider: "openrouter", models: ["default"] },
        { provider: "anthropic", models: ["sonnet"] },
      ]),
    ).toBe(
      "Your agents can't reply until MindRoom has an API key for OpenRouter and Anthropic.",
    );
  });

  it("preselects the provider the models already use", () => {
    expect(defaultProviderChoice(OPENROUTER_MISSING)).toBe("openrouter");
    expect(
      defaultProviderChoice([{ provider: "anthropic", models: ["sonnet"] }]),
    ).toBe("anthropic");
    expect(
      defaultProviderChoice([{ provider: "groq", models: ["fast"] }]),
    ).toBe("openrouter");
  });

  it("asks to switch models when a different provider was connected", () => {
    expect(remainingProviderHint("anthropic", [])).toBeNull();
    expect(remainingProviderHint("anthropic", OPENROUTER_MISSING)).toBe(
      "Anthropic key saved. Your agents still use OpenRouter for the default model. Switch it to Anthropic on the Models page, or connect OpenRouter too.",
    );
  });
});

describe("ProviderSetupBanner", () => {
  beforeEach(() => {
    mockToast.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("stays hidden when every used provider has a key", async () => {
    mockFetch(() => jsonResponse({ missing: [] }));

    renderBanner();

    await waitFor(() => expect(fetchCalls()).toHaveLength(1));
    expect(fetchCalls()[0][0]).toBe("/api/provider-setup/status");
    expect(screen.queryByText("Connect your AI provider.")).toBeNull();
  });

  it("stays hidden when the status check fails", async () => {
    mockFetch(() => jsonResponse({ detail: "Server error" }, 500));

    renderBanner();

    await waitFor(() => expect(fetchCalls()).toHaveLength(1));
    expect(screen.queryByText("Connect your AI provider.")).toBeNull();
  });

  it("verifies and saves an OpenRouter key, then hides", async () => {
    let connected = false;
    mockFetch((url, init) => {
      if (url === "/api/provider-setup/connect") {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({
          provider: "openrouter",
          api_key: "sk-or-v1-test",
        });
        connected = true;
        return jsonResponse({ service: "openrouter", missing: [] });
      }
      return jsonResponse({ missing: connected ? [] : OPENROUTER_MISSING });
    });

    renderBanner();

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect provider" }),
    );
    expect(
      screen.getByRole("radio", { name: /OpenRouter.*Recommended/ }),
    ).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByText(/One key covers chat, memory, and voice/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("OpenRouter API key"), {
      target: { value: "sk-or-v1-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Verify and save" }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith({
        title: "OpenRouter connected",
        description: "Your agents can reply now.",
      }),
    );
    await waitFor(() =>
      expect(screen.queryByText("Connect your AI provider.")).toBeNull(),
    );
  });

  it("shows the provider rejection without closing", async () => {
    mockFetch((url) =>
      url === "/api/provider-setup/connect"
        ? jsonResponse(
            {
              detail:
                "OpenRouter rejected this API key. Check that you copied the whole key and that it is active.",
            },
            400,
          )
        : jsonResponse({ missing: OPENROUTER_MISSING }),
    );

    renderBanner();

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect provider" }),
    );
    fireEvent.change(screen.getByLabelText("OpenRouter API key"), {
      target: { value: "sk-or-v1-wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Verify and save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "OpenRouter rejected this API key.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("points to the Models page after saving a key for another provider", async () => {
    mockFetch((url) =>
      url === "/api/provider-setup/connect"
        ? jsonResponse({ service: "anthropic", missing: OPENROUTER_MISSING })
        : jsonResponse({ missing: OPENROUTER_MISSING }),
    );

    renderBanner();

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect provider" }),
    );
    fireEvent.click(screen.getByRole("radio", { name: /Anthropic/ }));
    fireEvent.change(screen.getByLabelText("Anthropic API key"), {
      target: { value: "sk-ant-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Verify and save" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Anthropic key saved. Your agents still use OpenRouter for the default model.",
    );
    expect(
      screen.getByRole("link", { name: "Open the Models page" }),
    ).toHaveAttribute("href", "/models");
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("does not submit an empty key", async () => {
    mockFetch(() => jsonResponse({ missing: OPENROUTER_MISSING }));

    renderBanner();

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect provider" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Verify and save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Paste an API key first.",
    );
    expect(fetchCalls().map(([url]) => url)).toEqual([
      "/api/provider-setup/status",
    ]);
  });
});
