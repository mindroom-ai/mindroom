import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Credentials } from "./Credentials";

const mockToast = vi.fn();

vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// The egress panel has its own tests and fetches on mount, which would consume
// the ordered fetch mocks below. Only the tests of the whole page ask for the real one.
const realPanel = vi.hoisted(() => ({ wanted: false }));
vi.mock("./EgressBroker", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./EgressBroker")>();
  return {
    EgressBroker: () =>
      realPanel.wanted ? (
        <actual.EgressBroker />
      ) : (
        <div data-testid="egress-broker" />
      ),
  };
});

global.fetch = vi.fn();

/** The raw payloads start collapsed, so open them as a person would. */
function renderOpen() {
  render(<Credentials />);
  fireEvent.click(
    screen.getByRole("button", { name: "Raw credential payloads (advanced)" }),
  );
}

/** The raw editor's list is empty; the panel is mocked, so nothing else fetches. */
const serveNoServices = () =>
  (global.fetch as any).mockResolvedValue({ ok: true, json: async () => [] });

/** One service, github_private, holding a token: the list, its status, then its credentials. */
const serveGithubToken = () =>
  (global.fetch as any)
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ["github_private"],
    })
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        service: "github_private",
        has_credentials: true,
        key_names: ["token"],
      }),
    })
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        service: "github_private",
        credentials: { token: "ghp_test" },
      }),
    });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("Credentials", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
    realPanel.wanted = false;
  });

  it("loads services and selected service credentials", async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ["github_private"],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "github_private",
          has_credentials: true,
          key_names: ["username", "token"],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "github_private",
          credentials: { username: "x-access-token", token: "ghp_test" },
        }),
      });

    renderOpen();

    await waitFor(() => {
      expect(screen.getByText("Configured")).toBeInTheDocument();
      expect(screen.getByText("Keys: username, token")).toBeInTheDocument();
    });

    const editor = screen.getByPlaceholderText(
      '{"api_key":"..."}',
    ) as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"username": "x-access-token"');
      expect(editor.value).toContain('"token": "ghp_test"');
    });
  });

  it("puts Agent Keys before Raw credential payloads (advanced)", async () => {
    realPanel.wanted = true;
    (global.fetch as any).mockImplementation(async (input: string) => {
      const path = String(input).split("?")[0];
      const body =
        path === "/api/egress-broker/services"
          ? { services: [], shared_agents: [], has_unscoped_agents: false }
          : path === "/api/egress-broker/logs"
            ? { records: [] }
            : [];
      return { ok: true, status: 200, json: async () => body };
    });

    render(<Credentials />);

    const panel = await screen.findByRole("heading", {
      name: "Agent Keys",
    });
    const raw = screen.getByRole("heading", {
      name: "Raw credential payloads (advanced)",
    });
    expect(
      panel.compareDocumentPosition(raw) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.queryByText("Credentials Manager")).toBeNull();
  });

  it("shows no Agent Keys while the broker is off, and the rest of the page as before", async () => {
    realPanel.wanted = true;
    (global.fetch as any).mockImplementation(async (input: string) =>
      String(input).startsWith("/api/egress-broker/")
        ? {
            ok: false,
            status: 404,
            json: async () => ({ detail: "Agent Keys are not enabled" }),
          }
        : { ok: true, status: 200, json: async () => [] },
    );

    const egressRequests = () =>
      (global.fetch as any).mock.calls
        .map(([input]: [string]) => String(input))
        .filter((path: string) => path.startsWith("/api/egress-broker/"));

    render(<Credentials />);

    expect(
      await screen.findByRole("heading", {
        name: "Raw credential payloads (advanced)",
      }),
    ).toBeInTheDocument();
    await waitFor(() => expect(egressRequests()).toHaveLength(1));
    // Give a follow-up request, or the card, the chance to appear.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(egressRequests()).toEqual(["/api/egress-broker/services"]);
    expect(screen.queryByRole("heading", { name: "Agent Keys" })).toBeNull();
    expect(screen.queryByText(/not enabled/)).toBeNull();
  });

  it("keeps the raw editor's error under the raw editor, after the panel", async () => {
    (global.fetch as any).mockRejectedValue(new Error("Failed to load"));

    render(<Credentials />);

    const error = await screen.findByText("Failed to load");
    const panel = screen.getByTestId("egress-broker");
    const raw = screen.getByRole("heading", {
      name: "Raw credential payloads (advanced)",
    });
    expect(
      panel.compareDocumentPosition(raw) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      raw.compareDocumentPosition(error) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("collapses the raw payloads until the heading is opened", async () => {
    serveNoServices();

    render(<Credentials />);

    const toggle = screen.getByRole("button", {
      name: "Raw credential payloads (advanced)",
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByPlaceholderText('{"api_key":"..."}')).toBeNull();
    expect(screen.queryByRole("button", { name: "Save payload" })).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByPlaceholderText('{"api_key":"..."}'),
    ).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.queryByPlaceholderText('{"api_key":"..."}')).toBeNull();
  });

  it("hides the credentials again when the raw payloads are collapsed", async () => {
    serveGithubToken();

    renderOpen();
    const editor = (await screen.findByPlaceholderText(
      '{"api_key":"..."}',
    )) as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toContain("ghp_test"));
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByRole("button", { name: "Hide" })).toBeInTheDocument();
    expect(editor).not.toHaveClass("blur-sm");

    const toggle = screen.getByRole("button", {
      name: "Raw credential payloads (advanced)",
    });
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    // Opened again, the payload is hidden as it is on first opening.
    expect(screen.getByRole("button", { name: "Show" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText('{"api_key":"..."}')).toHaveClass(
      "blur-sm",
    );
  });

  it("keeps the heading's chevron still for someone who prefers reduced motion", () => {
    serveNoServices();
    render(<Credentials />);
    const chevron = screen
      .getByRole("button", { name: "Raw credential payloads (advanced)" })
      .querySelector("svg");
    expect(chevron).toHaveClass("motion-reduce:transition-none");
  });

  it("points the heading at the payloads only while they are shown", async () => {
    serveNoServices();
    render(<Credentials />);
    const toggle = screen.getByRole("button", {
      name: "Raw credential payloads (advanced)",
    });
    expect(toggle).not.toHaveAttribute("aria-controls");
    fireEvent.click(toggle);
    expect(
      document.getElementById(toggle.getAttribute("aria-controls") ?? ""),
    ).not.toBeNull();
    fireEvent.click(toggle);
    expect(toggle).not.toHaveAttribute("aria-controls");
  });

  it("names the raw editor's save button Save payload", async () => {
    serveNoServices();

    renderOpen();

    await screen.findByText(/No services found yet/);
    expect(
      screen.getByRole("button", { name: "Save payload" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("hides credentials by default and reveals them on demand", async () => {
    serveGithubToken();

    renderOpen();

    const editor = screen.getByPlaceholderText(
      '{"api_key":"..."}',
    ) as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"token": "ghp_test"');
      expect(editor.className).toContain("blur-sm");
      expect(
        screen.getByText(
          "Credentials hidden for screen sharing. Click Show to reveal.",
        ),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Show" }));

    await waitFor(() => {
      expect(editor.className).not.toContain("blur-sm");
      expect(
        screen.queryByText(
          "Credentials hidden for screen sharing. Click Show to reveal.",
        ),
      ).toBeNull();
      expect(screen.getByRole("button", { name: "Hide" })).toBeInTheDocument();
    });
  });

  it("saves credentials JSON for selected service", async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ["github_private"],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "github_private",
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "github_private",
          credentials: {},
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: "success",
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "github_private",
          has_credentials: true,
          key_names: ["username", "token"],
        }),
      });

    renderOpen();

    await screen.findByText("Empty");
    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    const editor = screen.getByPlaceholderText('{"api_key":"..."}');
    fireEvent.change(editor, {
      target: { value: '{"username":"x-access-token","token":"ghp_updated"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save payload" }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/github_private",
        expect.objectContaining({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            credentials: { username: "x-access-token", token: "ghp_updated" },
          }),
        }),
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: "Credentials saved",
        description: "Updated credentials for 'github_private'.",
      });
    });
  });

  it("follows the canonical service when the backend renames a provider key", async () => {
    (global.fetch as any).mockImplementation(
      async (url: string, init?: RequestInit) => {
        if (url === "/api/credentials/list") {
          return { ok: true, json: async () => [] };
        }
        if (url === "/api/credentials/ANTHROPIC_API_KEY") {
          if (init?.method === "POST") {
            return {
              ok: true,
              json: async () => ({ status: "success", service: "anthropic" }),
            };
          }
          return {
            ok: true,
            json: async () => ({
              service: "ANTHROPIC_API_KEY",
              credentials: {},
            }),
          };
        }
        if (url === "/api/credentials/anthropic/status") {
          return {
            ok: true,
            json: async () => ({
              service: "anthropic",
              has_credentials: true,
              key_names: ["api_key"],
            }),
          };
        }
        if (url === "/api/credentials/anthropic") {
          return {
            ok: true,
            json: async () => ({
              service: "anthropic",
              credentials: { api_key: "sk-ant-dashboard" },
            }),
          };
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    renderOpen();

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(1);
    });
    fireEvent.change(screen.getByPlaceholderText("new_service_name"), {
      target: { value: "ANTHROPIC_API_KEY" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.change(screen.getByPlaceholderText('{"api_key":"..."}'), {
      target: { value: '{"api_key":"sk-ant-dashboard"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save payload" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "Credentials saved",
        description:
          "Saved as 'anthropic', the service MindRoom reads for 'ANTHROPIC_API_KEY'.",
      });
      expect(screen.getByText("Keys: api_key")).toBeInTheDocument();
    });
    expect(screen.getAllByText("anthropic").length).toBeGreaterThan(0);
    expect(screen.queryByText("ANTHROPIC_API_KEY")).toBeNull();
    const requestedUrls = (global.fetch as any).mock.calls.map(
      ([url]: [string]) => url,
    );
    expect(requestedUrls).toContain("/api/credentials/anthropic");
  });

  it("shows error and does not save when JSON is invalid", async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ["github_private"],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "github_private",
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "github_private",
          credentials: {},
        }),
      });

    renderOpen();

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.change(screen.getByPlaceholderText('{"api_key":"..."}'), {
      target: { value: '{"broken_json": ' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save payload" }));

    await waitFor(() => {
      expect(
        screen.getByText("Credentials must be valid JSON"),
      ).toBeInTheDocument();
    });
    expect((global.fetch as any).mock.calls).toHaveLength(3);
  });

  it("deletes selected service credentials", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);

    serveGithubToken().mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        status: "success",
      }),
    });

    renderOpen();

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith(
        "Delete credentials for 'github_private'?",
      );
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/github_private",
        expect.objectContaining({
          method: "DELETE",
        }),
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: "Credentials deleted",
        description: "Removed credentials for 'github_private'.",
      });
    });
  });

  it("tests credentials for selected service", async () => {
    serveGithubToken().mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        message: "Credentials exist (validation not implemented)",
      }),
    });

    renderOpen();

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/github_private/test",
        expect.objectContaining({
          method: "POST",
        }),
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: "Credentials check",
        description: "Credentials exist (validation not implemented)",
      });
    });
  });

  it("shows error when creating a service with an invalid name", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => [],
    });

    renderOpen();

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(1);
    });

    fireEvent.change(screen.getByPlaceholderText("new_service_name"), {
      target: { value: "bad/name" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Service name can only include letters, numbers, colon, underscore, and hyphen",
        ),
      ).toBeInTheDocument();
    });
  });

  it("continues loading services when one status request fails", async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ["good_service", "zbad_service"],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "good_service",
          has_credentials: true,
          key_names: ["token"],
        }),
      })
      .mockRejectedValueOnce(new Error("Status endpoint failed"))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "good_service",
          credentials: { token: "abc" },
        }),
      });

    renderOpen();

    await waitFor(() => {
      expect(screen.getByText("good_service")).toBeInTheDocument();
      expect(screen.getByText("zbad_service")).toBeInTheDocument();
      expect(
        screen.getByText(
          "Some service statuses could not be loaded. You can still edit credentials.",
        ),
      ).toBeInTheDocument();
    });
  });

  it("prompts before switching services with unsaved changes", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ["service_a", "service_b"],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "service_a",
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "service_b",
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "service_a",
          credentials: {},
        }),
      });

    renderOpen();

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(4);
    });

    fireEvent.change(screen.getByPlaceholderText('{"api_key":"..."}'), {
      target: { value: '{"token":"draft"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /service_b/i }));

    expect(confirmSpy).toHaveBeenCalledWith(
      "Discard unsaved changes for 'service_a'?",
    );
    expect((global.fetch as any).mock.calls).toHaveLength(4);
  });

  it("ignores stale credentials response from previously selected service", async () => {
    const firstCredentials = deferred<any>();

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ["service_a", "service_b"],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "service_a",
          has_credentials: true,
          key_names: ["token"],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "service_b",
          has_credentials: true,
          key_names: ["token"],
        }),
      })
      .mockImplementationOnce(() => firstCredentials.promise)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: "service_b",
          credentials: { token: "b-value" },
        }),
      });

    renderOpen();

    const serviceBButton = await screen.findByRole("button", {
      name: /service_b/i,
    });
    await waitFor(() => {
      // The fourth request is service A's pending credential load; switch only after that stale request starts.
      expect((global.fetch as any).mock.calls).toHaveLength(4);
    });

    fireEvent.click(serviceBButton);

    const editor = screen.getByPlaceholderText(
      '{"api_key":"..."}',
    ) as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"token": "b-value"');
    });

    firstCredentials.resolve({
      ok: true,
      json: async () => ({
        service: "service_a",
        credentials: { token: "a-value" },
      }),
    });

    await waitFor(() => {
      expect(editor.value).toContain('"token": "b-value"');
      expect(editor.value).not.toContain('"token": "a-value"');
    });
  });
});
