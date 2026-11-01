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

/** The answer to a read of `path` from a backend holding these services, each with the token `<service>-value`. */
const serviceResponse = (names: string[], path: string) => {
  const [, , , service, suffix] = path.split("/");
  const body =
    path === "/api/credentials/list"
      ? names
      : suffix === "status"
        ? { service, has_credentials: true, key_names: ["token"] }
        : { service, credentials: { token: `${service}-value` } };
  return { ok: true, json: async () => body };
};

/** Serves these services; a request `fails(path)` names is rejected with that message. */
const serveServices = (
  names: string[],
  fails: (path: string) => string | undefined,
) =>
  (global.fetch as any).mockImplementation(async (input: string) => {
    const path = String(input);
    const failure = fails(path);
    if (failure) {
      throw new Error(failure);
    }
    return serviceResponse(names, path);
  });

/** Waits until the raw editor shows the token `serviceResponse` gives this service. */
const showsTokenOf = (service: string) =>
  waitFor(() => {
    expect(
      (screen.getByPlaceholderText('{"api_key":"..."}') as HTMLTextAreaElement)
        .value,
    ).toContain(`"token": "${service}-value"`);
  });

/** How many plain reads (no method) of this exact path the page made. */
const readsOf = (path: string) =>
  (global.fetch as any).mock.calls.filter(
    ([input, init]: [string, RequestInit | undefined]) =>
      input === path && init?.method === undefined,
  ).length;

/** Opens service_a, types a draft into the raw editor, and returns the editor. */
async function draftForServiceA(names: string[]) {
  serveServices(names, () => undefined);
  renderOpen();
  await showsTokenOf("service_a");
  const editor = screen.getByPlaceholderText(
    '{"api_key":"..."}',
  ) as HTMLTextAreaElement;
  fireEvent.change(editor, { target: { value: '{"token":"draft"}' } });
  return editor;
}

/** Types a service name into the new-service field and presses Add. */
const addService = (name: string) => {
  fireEvent.change(screen.getByPlaceholderText("new_service_name"), {
    target: { value: name },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
};

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
    addService("ANTHROPIC_API_KEY");
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

  it("deletes selected service credentials and reloads the list", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);

    serveGithubToken()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: "success",
          message: "Credentials deleted for github_private",
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ service: "github_private", credentials: {} }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => [] });

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
        title: "Delete credentials",
        description: "Credentials deleted for github_private",
      });
    });
    await waitFor(() => {
      expect(screen.queryByText("github_private")).not.toBeInTheDocument();
    });
  });

  it("keeps a service the environment still supplies selected after deleting it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const message =
      "Credentials for openai now come from the OPENAI_API_KEY environment variable; " +
      "to remove them, edit it in the environment or .env and restart MindRoom.";
    const keys: Record<string, string> = {
      anthropic: "sk-ant-env-dummy",
      openai: "sk-env-dummy",
    };
    (global.fetch as any).mockImplementation(
      async (input: string, init?: RequestInit) => {
        if (init?.method === "DELETE") {
          return {
            ok: true,
            json: async () => ({ status: "success", message }),
          };
        }
        const path = String(input);
        const [, , , service, suffix] = path.split("/");
        const body =
          path === "/api/credentials/list"
            ? Object.keys(keys)
            : suffix === "status"
              ? { service, has_credentials: true, key_names: ["api_key"] }
              : { service, credentials: { api_key: keys[service] } };
        return { ok: true, json: async () => body };
      },
    );
    renderOpen();

    // The page selects the first service itself, and a click made before that runs is overridden.
    await screen.findByText("Active: anthropic");
    fireEvent.click(screen.getByRole("button", { name: /^openai/ }));
    await screen.findByText("Active: openai");
    await waitFor(() => {
      expect(readsOf("/api/credentials/openai")).toBe(1);
    });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "Delete credentials",
        description: message,
      });
    });
    expect(readsOf("/api/credentials/list")).toBe(2);
    expect(readsOf("/api/credentials/openai")).toBe(2);
    expect(screen.getByText("Active: openai")).toBeInTheDocument();
  });

  it("keeps a service selected during a delete usable", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const deletion = deferred<any>();
    (global.fetch as any).mockImplementation(
      async (input: string, init?: RequestInit) =>
        init?.method === "DELETE"
          ? deletion.promise
          : serviceResponse(["service_a", "service_b"], String(input)),
    );

    renderOpen();

    await screen.findByText("Active: service_a");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: /^service_b/ }));
    const editor = screen.getByPlaceholderText(
      '{"api_key":"..."}',
    ) as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"token": "service_b-value"');
    });
    deletion.resolve({
      ok: true,
      json: async () => ({
        status: "success",
        message: "Credentials deleted for service_a",
      }),
    });

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "Delete credentials",
        description: "Credentials deleted for service_a",
      });
    });
    expect(screen.getByText("Active: service_b")).toBeInTheDocument();
    expect(editor).not.toBeDisabled();
  });

  it("keeps the list error when the reload after a delete fails", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let deleted = false;
    (global.fetch as any).mockImplementation(
      async (input: string, init?: RequestInit) => {
        const path = String(input);
        if (init?.method === "DELETE") {
          deleted = true;
          return {
            ok: true,
            json: async () => ({
              status: "success",
              message: "Credentials deleted for github_private",
            }),
          };
        }
        if (path === "/api/credentials/list" && deleted) {
          throw new Error("Service list unavailable");
        }
        const body =
          path === "/api/credentials/list"
            ? ["github_private"]
            : path.endsWith("/status")
              ? {
                  service: "github_private",
                  has_credentials: true,
                  key_names: ["token"],
                }
              : { service: "github_private", credentials: {} };
        return { ok: true, json: async () => body };
      },
    );

    renderOpen();

    await screen.findByText("Active: github_private");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "Delete credentials",
        description: "Credentials deleted for github_private",
      });
    });
    expect(screen.getByText("Service list unavailable")).toBeInTheDocument();
  });

  it("keeps the credentials error when the reload after a delete cannot read them", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let deleted = false;
    (global.fetch as any).mockImplementation(
      async (input: string, init?: RequestInit) => {
        const path = String(input);
        if (init?.method === "DELETE") {
          deleted = true;
          return {
            ok: true,
            json: async () => ({ status: "success", message: "Deleted" }),
          };
        }
        if (path === "/api/credentials/github_private" && deleted) {
          throw new Error("Credentials unavailable");
        }
        const body =
          path === "/api/credentials/list"
            ? ["github_private"]
            : path.endsWith("/status")
              ? {
                  service: "github_private",
                  has_credentials: true,
                  key_names: ["token"],
                }
              : { service: "github_private", credentials: { token: "t" } };
        return { ok: true, json: async () => body };
      },
    );

    renderOpen();

    await screen.findByText("Active: github_private");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "Delete credentials",
        description: "Deleted",
      });
    });
    expect(screen.getByText("Credentials unavailable")).toBeInTheDocument();
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

    addService("bad/name");

    await waitFor(() => {
      expect(
        screen.getByText(
          "Service name can only include letters, numbers, colon, underscore, and hyphen",
        ),
      ).toBeInTheDocument();
    });
  });

  /** zbad_service's status request fails while `statusFails()` says so. */
  const serveOneFailingStatus = (statusFails: () => boolean) =>
    serveServices(["good_service", "other_service", "zbad_service"], (path) =>
      path === "/api/credentials/zbad_service/status" && statusFails()
        ? "Status endpoint failed"
        : undefined,
    );
  const PARTIAL_WARNING =
    "Some service statuses could not be loaded. You can still edit credentials.";

  it("continues loading services when one status request fails, and keeps its warning", async () => {
    serveOneFailingStatus(() => true);

    renderOpen();

    await showsTokenOf("good_service");
    expect(
      screen.getByRole("button", { name: /^zbad_service/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(PARTIAL_WARNING)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^other_service/ }));

    await showsTokenOf("other_service");
    expect(screen.getByText(PARTIAL_WARNING)).toBeInTheDocument();
  });

  it("shows the list's warning above the selected service's own error", async () => {
    serveServices(["good_service", "zbad_service"], (path) =>
      path.startsWith("/api/credentials/zbad_service")
        ? "zbad_service unreadable"
        : undefined,
    );

    renderOpen();

    await showsTokenOf("good_service");
    fireEvent.click(screen.getByRole("button", { name: /^zbad_service/ }));

    const serviceError = await screen.findByText("zbad_service unreadable");
    expect(
      screen.getByText(PARTIAL_WARNING).compareDocumentPosition(serviceError) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("drops the partial-status warning once every status loads on Refresh", async () => {
    let statusFails = true;
    serveOneFailingStatus(() => statusFails);

    renderOpen();

    await screen.findByText(PARTIAL_WARNING);
    statusFails = false;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(screen.queryByText(PARTIAL_WARNING)).not.toBeInTheDocument();
    });
  });

  it("keeps the list error when the list fails to reload on Refresh", async () => {
    let refreshed = false;
    serveServices(["service_a", "service_b"], (path) =>
      path === "/api/credentials/list" && refreshed
        ? "Service list unavailable"
        : undefined,
    );

    renderOpen();

    await screen.findByText("Active: service_a");
    await waitFor(() => {
      expect(readsOf("/api/credentials/service_a")).toBe(1);
    });
    refreshed = true;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(readsOf("/api/credentials/service_a")).toBe(2);
    });
    expect(
      await screen.findByText("Service list unavailable"),
    ).toBeInTheDocument();

    // The stale list stays usable, and working with it keeps the list's error.
    fireEvent.click(screen.getByRole("button", { name: /^service_b/ }));
    await showsTokenOf("service_b");
    expect(screen.getByText("Service list unavailable")).toBeInTheDocument();
  });

  it("reloads only the list on Refresh when there is no service", async () => {
    serveNoServices();

    renderOpen();

    await screen.findByText(
      "No services found yet. Add one and save JSON credentials.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(readsOf("/api/credentials/list")).toBe(2);
    });
    expect((global.fetch as any).mock.calls).toHaveLength(2);
  });

  it("clears the previous service's error when another service is selected", async () => {
    serveServices(["service_a", "service_b"], (path) =>
      path === "/api/credentials/service_a"
        ? "Credentials unavailable"
        : undefined,
    );

    renderOpen();

    expect(
      await screen.findByText("Credentials unavailable"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^service_b/ }));

    await showsTokenOf("service_b");
    expect(
      screen.queryByText("Credentials unavailable"),
    ).not.toBeInTheDocument();
  });

  it("prompts before switching services with unsaved changes", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const editor = await draftForServiceA(["service_a", "service_b"]);

    fireEvent.click(screen.getByRole("button", { name: /^service_b/ }));

    expect(confirmSpy).toHaveBeenCalledWith(
      "Discard unsaved changes for 'service_a'?",
    );
    expect(readsOf("/api/credentials/service_b")).toBe(0);
    expect(editor.value).toBe('{"token":"draft"}');
  });

  it("prompts before Add discards unsaved changes, and keeps them when declined", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const editor = await draftForServiceA(["service_a", "service_b"]);

    // Adding the selected service again replaces nothing, so it does not ask.
    addService("service_a");
    expect(confirmSpy).not.toHaveBeenCalled();

    addService("service_b");
    expect(confirmSpy).toHaveBeenLastCalledWith(
      "Discard unsaved changes for 'service_a'?",
    );
    expect(screen.getByText("Active: service_a")).toBeInTheDocument();
    expect(editor.value).toBe('{"token":"draft"}');

    addService("new_service");
    expect(confirmSpy).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Active: service_a")).toBeInTheDocument();
    expect(editor.value).toBe('{"token":"draft"}');
    expect(screen.queryByRole("button", { name: /^new_service/ })).toBeNull();

    confirmSpy.mockReturnValue(true);
    addService("new_service");
    expect(screen.getByText("Active: new_service")).toBeInTheDocument();
    expect(editor.value).toBe("{}");
  });

  it("prompts before Refresh discards unsaved changes, and keeps them when declined", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const editor = await draftForServiceA(["service_a"]);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(confirmSpy).toHaveBeenCalledWith(
      "Discard unsaved changes for 'service_a'?",
    );
    // Refresh reads the service's credentials first, so a reload would show here at once.
    expect(readsOf("/api/credentials/service_a")).toBe(1);
    expect(editor.value).toBe('{"token":"draft"}');
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
