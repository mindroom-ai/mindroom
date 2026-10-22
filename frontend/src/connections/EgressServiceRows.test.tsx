import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  credentialService,
  expectNoAction,
  oauthStatus,
  pressInDialog,
  rowAction,
  rowControls,
  ruleSummaries,
  wholeText,
} from "@/test/egressServiceTestKit";
import { githubRepositoryRules } from "./githubRepositoryRules";
import type { RowAgent, ServiceEditing } from "./egressRowText";
import { EgressServiceRows as Rows } from "./EgressServiceRows";
import { EgressServiceTable } from "./EgressServiceTable";
import type {
  EgressCredentialService,
  EgressOAuthStatus,
  EgressRuleAuthType,
  EgressRuleSummary,
} from "./types";

type RowsProps =
  ComponentProps<typeof Rows> extends infer P
    ? P extends { agentName?: infer Name }
      ? Omit<P, "agent" | "agentName" | "onChanged" | "serviceEditing"> & {
          agent?: RowAgent;
          agentName?: Name;
          onChanged?: () => void;
          serviceEditing?: ServiceEditing;
        }
      : never
    : never;

/**
 * The rows as a page renders them: for the agent "personal", whose rows say
 * Personal Mind, with the user's own services editable, unless a test passes
 * another target, agent, or service editing.
 */
function EgressServiceRows({ onChanged = vi.fn(), ...props }: RowsProps) {
  const agent = props.agent ?? {
    displayName: "Personal Mind",
    shared: props.services.some((service) => service.is_shared === true),
    services: props.services,
  };
  const target = props.secretPath ? props : { agentName: "personal", ...props };
  return (
    <EgressServiceTable label="Services">
      <Rows
        {...(target as ComponentProps<typeof Rows>)}
        onChanged={onChanged}
        agent={agent}
        serviceEditing={props.serviceEditing ?? editing()}
      />
    </EgressServiceTable>
  );
}
const renderRows = (
  services: EgressCredentialService[],
  props: Omit<RowsProps, "services"> = {},
) =>
  render(<EgressServiceRows {...(props as RowsProps)} services={services} />);

const github = credentialService();
const openai: EgressCredentialService = {
  ...github,
  name: "openai",
  display_name: "OpenAI",
  description: "OpenAI API",
  configured: true,
  active_source: "key",
  key_configured: true,
};
const githubAccount = oauthStatus();
const withAccount = (
  account: Partial<EgressOAuthStatus> = {},
  service: Partial<EgressCredentialService> = {},
): EgressCredentialService => ({
  ...github,
  oauth: { ...githubAccount, ...account },
  ...service,
});
const connectedGithub = withAccount(
  { connected: true, account_label: "octocat" },
  { configured: true, active_source: "oauth" },
);
const authorization = {
  provider: "github",
  auth_url: "https://github.com/login/oauth/authorize?state=abc",
  completion_origin: "https://portal.example.com",
};

const noContent = () => new Response(null, { status: 204 });
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const row = (name: string) => screen.getByRole("row", { name });

/** Service editing for the viewer's own services, which says nothing on delete unless a test passes a warning. */
const editing = (overrides: Partial<ServiceEditing> = {}): ServiceEditing => ({
  editableSource: "user",
  onEdit: vi.fn(),
  onDelete: vi.fn(async () => undefined),
  deleteWarning: () => "",
  ...overrides,
});

type FakePopup = {
  closed: boolean;
  close: ReturnType<typeof vi.fn>;
  location: { href: string };
  document: Document;
};
/** The window a connection opens, faked so a test can read and close it. */
let popup: FakePopup;
function fakePopup() {
  popup = {
    closed: false,
    close: vi.fn(),
    location: { href: "about:blank" },
    document: document.implementation.createHTMLDocument(),
  };
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
}
/** What the popup's own page posts back when the provider is done. */
const answer = (status: string) =>
  window.dispatchEvent(
    new MessageEvent("message", {
      origin: authorization.completion_origin,
      source: popup as unknown as Window,
      data: { type: "mindroom-oauth", provider: "github", status },
    }),
  );
/** Render a GitHub row that can connect, press Connect, and wait until the popup shows the provider's page. */
async function startConnect(onChanged = vi.fn()) {
  renderRows([withAccount()], { onChanged });
  fireEvent.click(
    screen.getByRole("button", {
      name: "Connect GitHub for Agent Keys for GitHub",
    }),
  );
  await waitFor(() => expect(popup.location.href).toBe(authorization.auth_url));
  expect(screen.getByText("Connecting…")).toBeVisible();
  return onChanged;
}

beforeEach(() => {
  vi.mocked(fetch).mockImplementation(async () => noContent());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("egress service rows", () => {
  it("shows name, description and the Key saved or Not set status", async () => {
    renderRows([github, openai]);
    expect(within(row("GitHub")).getByText("Not set")).toBeInTheDocument();
    expect(
      within(row("GitHub")).getByText("GitHub API and git over HTTPS"),
    ).toBeInTheDocument();
    expect(within(row("OpenAI")).getByText("Key saved")).toBeInTheDocument();
    expect(
      within(row("GitHub")).getByRole("button", { name: "Set key for GitHub" }),
    ).toBeInTheDocument();
    expect(
      await rowAction("Replace key for OpenAI", row("OpenAI")),
    ).toBeInTheDocument();
  });

  it("saves the key, clears the input and reports the change", async () => {
    const onChanged = vi.fn();
    renderRows([github], { onChanged });
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    const input = screen.getByLabelText("GitHub API key");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/egress/agents/personal/github",
      expect.objectContaining({
        method: "PUT",
        credentials: "same-origin",
        body: JSON.stringify({ secret: "abc" }),
      }),
    );
    expect(screen.queryByLabelText("GitHub API key")).toBeNull();
    // Reopening the editor must not show the previous value.
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    expect(screen.getByLabelText("GitHub API key")).toHaveValue("");
  });

  it("encodes the agent and service names in the request path", async () => {
    renderRows([{ ...github, name: "git/hub" }], { agentName: "my agent" });
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "/api/connections/egress/agents/my%20agent/git%2Fhub",
    );
  });

  it("writes to a custom secret path instead of the connections API", async () => {
    renderRows([github], {
      secretPath: (name) => `/custom/${name}/secret`,
      servicesPath: "/custom/services",
      accountPath: (name, action) => `/custom/${name}/${action}`,
    });
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/custom/github/secret");
  });

  it("cancels without sending anything and drops the typed value", async () => {
    renderRows([openai]);
    fireEvent.click(await rowAction("Replace key for OpenAI"));
    fireEvent.change(screen.getByLabelText("OpenAI API key"), {
      target: { value: "sk-typed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("OpenAI API key")).toBeNull();
    fireEvent.click(await rowAction("Replace key for OpenAI"));
    expect(screen.getByLabelText("OpenAI API key")).toHaveValue("");
  });

  it("keeps Save disabled until a key is typed", () => {
    renderRows([github]);
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "   " },
    });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("shows the server validation message and keeps the editor open on failure", async () => {
    const onChanged = vi.fn();
    vi.mocked(fetch).mockImplementation(async () =>
      json({ detail: "Secret cannot be empty or whitespace" }, 422),
    );
    renderRows([github], { onChanged });
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Secret cannot be empty or whitespace",
    );
    expect(onChanged).not.toHaveBeenCalled();
    expect(screen.getByLabelText("GitHub API key")).toBeInTheDocument();
  });

  it("removes a key only after confirmation", async () => {
    const onChanged = vi.fn();
    renderRows([openai], { onChanged });
    fireEvent.click(await rowAction("Remove key for OpenAI"));
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/egress/agents/personal/openai",
      expect.objectContaining({
        method: "DELETE",
        credentials: "same-origin",
      }),
    );
  });

  it("does not remove the key when the confirmation is cancelled", async () => {
    renderRows([openai]);
    fireEvent.click(await rowAction("Remove key for OpenAI"));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("gives focus back to the row's menu button when its dialog is cancelled", async () => {
    renderRows([openai]);
    fireEvent.click(await rowAction("Remove key for OpenAI"));
    await pressInDialog("Cancel");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "More actions for OpenAI" }),
      ).toHaveFocus(),
    );
  });

  it("offers no Remove for a service without a key", async () => {
    renderRows([github]);
    await expectNoAction(/Remove/);
  });

  it("shows status only to users who cannot manage the shared key", () => {
    renderRows([{ ...openai, is_shared: true, can_manage: false }], {
      agentName: "shared_dev",
    });
    expect(within(row("OpenAI")).getByText("Key saved")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("warns that a shared key affects everyone who relies on it", async () => {
    renderRows([{ ...openai, is_shared: true, can_manage: true }], {
      agentName: "shared_dev",
    });
    fireEvent.click(await rowAction("Remove key for OpenAI"));
    expect(
      await screen.findByText(
        "This deletes the shared key. Everyone who relies on it loses access to this service until a key is set again.",
      ),
    ).toBeInTheDocument();
  });

  it("warns that removing a personal key cuts off the agent until a key is set again", async () => {
    renderRows([openai]);
    fireEvent.click(await rowAction("Remove key for OpenAI"));
    expect(
      await screen.findByText(
        "This deletes your saved key. Your agent loses access to this service until you set a key again.",
      ),
    ).toBeInTheDocument();
  });

  it("reports a forbidden save with the connections wording by default", async () => {
    vi.mocked(fetch).mockImplementation(async () => json({}, 403));
    renderRows([github]);
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "Connections are not available for this account.",
      ),
    ).toBeInTheDocument();
  });

  it("uses the supplied wording for 403 and 404 responses", async () => {
    renderRows([openai], {
      secretPath: (name) => `/custom/${name}/secret`,
      servicesPath: "/custom/services",
      accountPath: (name, action) => `/custom/${name}/${action}`,
      errorMessages: { forbidden: "No access.", notFound: "Gone." },
    });
    fireEvent.click(await rowAction("Remove key for OpenAI"));
    vi.mocked(fetch).mockImplementation(async () => json({}, 404));
    await pressInDialog("Remove");
    expect(await screen.findByText("Gone.")).toBeInTheDocument();

    vi.mocked(fetch).mockImplementation(async () => json({}, 403));
    fireEvent.click(await rowAction("Remove key for OpenAI"));
    await pressInDialog("Remove");
    expect(await screen.findByText("No access.")).toBeInTheDocument();
  });
});

describe("connected accounts", () => {
  beforeEach(fakePopup);

  it("offers Connect first and keeps the key behind a secondary toggle", async () => {
    renderRows([withAccount()]);
    const github = row("GitHub");
    expect(within(github).getByText("Not set")).toBeInTheDocument();
    expect(
      within(github).getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    ).toBeEnabled();
    await expectNoAction("Set key for GitHub", github);

    fireEvent.click(
      await rowAction("Use an API key instead for GitHub", github),
    );
    expect(within(github).getByLabelText("GitHub API key")).toBeInTheDocument();
    await expectNoAction("Use an API key instead for GitHub", github);
    // Cancelling folds the key controls away again.
    fireEvent.click(within(github).getByRole("button", { name: "Cancel" }));
    expect(
      await rowAction("Use an API key instead for GitHub", github),
    ).toBeInTheDocument();
  });

  it("opens the popup, calls the connect endpoint and reports the change", async () => {
    vi.mocked(fetch).mockImplementation(async () => json(authorization));
    const onChanged = vi.fn();
    renderRows([withAccount()], { agentName: "my agent", onChanged });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    );

    await waitFor(() =>
      expect(popup.location.href).toBe(authorization.auth_url),
    );
    expect(window.open).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/egress/agents/my%20agent/github/connect",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    expect(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    ).toBeDisabled();
    expect(onChanged).not.toHaveBeenCalled();

    answer("connected");
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    // The page that answered closes itself.
    expect(popup.close).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    ).toBeEnabled();
  });

  describe("when the popup's own page answers", () => {
    beforeEach(() => {
      vi.mocked(fetch).mockImplementation(async () => json(authorization));
    });

    it("stops Connecting when the user cancelled, without waiting for the popup to close", async () => {
      const onChanged = await startConnect();
      answer("cancelled");
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Authorization was cancelled.",
      );
      expect(screen.queryByText("Connecting…")).toBeNull();
      expect(
        screen.getByRole("button", {
          name: "Connect GitHub for Agent Keys for GitHub",
        }),
      ).toBeEnabled();
      expect(onChanged).not.toHaveBeenCalled();
      // The page closes itself, so the row leaves the popup alone.
      expect(popup.close).not.toHaveBeenCalled();
    });

    it("stops Connecting and says so when the provider reported an error", async () => {
      const onChanged = await startConnect();
      answer("error");
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Could not connect. Try again.",
      );
      expect(screen.queryByText("Connecting…")).toBeNull();
      expect(onChanged).not.toHaveBeenCalled();
    });

    it("reports the change when it says connected", async () => {
      const onChanged = await startConnect();
      answer("connected");
      await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
      expect(screen.queryByText("Connecting…")).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
    });
  });

  it("uses the supplied account path to allow the account", async () => {
    renderRows(
      [
        withAccount(
          { connected: true, reuse_enabled: false },
          { configured: false },
        ),
      ],
      {
        secretPath: (name) => `/custom/${name}/secret`,
        servicesPath: "/custom/services",
        accountPath: (name, action) => `/custom/${name}/${action}?agent_name=a`,
      },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Use account for GitHub" }),
    );
    await pressInDialog("Use account");
    await waitFor(() =>
      expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
        "/custom/github/oauth-reuse?agent_name=a",
      ),
    );
  });

  it("uses the supplied account paths for connect and disconnect", async () => {
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes("/connect")
        ? json(authorization)
        : json({ status: "disconnected", provider: "github" }),
    );
    const onChanged = vi.fn();
    const { rerender } = renderRows([withAccount()], {
      secretPath: (name) => `/custom/${name}/secret`,
      servicesPath: "/custom/services",
      accountPath: (name, action) => `/custom/${name}/${action}?agent_name=a`,
      onChanged,
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    );
    await waitFor(() =>
      expect(popup.location.href).toBe(authorization.auth_url),
    );
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "/custom/github/connect?agent_name=a",
    );
    answer("connected");
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));

    rerender(
      <EgressServiceRows
        secretPath={(name) => `/custom/${name}/secret`}
        servicesPath="/custom/services"
        accountPath={(name, action) => `/custom/${name}/${action}?agent_name=a`}
        services={[connectedGithub]}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(await rowAction("Disconnect for GitHub"));
    await pressInDialog("Disconnect");
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(2));
    expect(vi.mocked(fetch).mock.calls[1][0]).toBe(
      "/custom/github/disconnect?agent_name=a",
    );
  });

  it("shows the server error and does not report a change when connecting fails", async () => {
    vi.mocked(fetch).mockImplementation(async () => json({}, 409));
    const onChanged = vi.fn();
    renderRows([withAccount()], { onChanged });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not complete the request. Try again.",
    );
    expect(onChanged).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    ).toBeEnabled();
  });

  it("tells the user when the browser blocks the popup", async () => {
    vi.mocked(window.open).mockReturnValue(null);
    renderRows([withAccount()]);
    fireEvent.click(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The popup was blocked",
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("offers Connect only when the server says the account is connectable", async () => {
    renderRows([withAccount({ can_connect: false })]);
    expect(screen.queryByRole("button", { name: /Connect/ })).toBeNull();
    // With no account to connect, the key is the row's main action.
    expect(
      screen.getByRole("button", { name: "Set key for GitHub" }),
    ).toBeEnabled();
    await expectNoAction(/Use an API key instead/);
  });

  it("lets a user without key rights connect a requester-scoped account", async () => {
    renderRows([withAccount({}, { is_shared: true, can_manage: false })], {
      agentName: "shared_dev",
    });
    expect(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    ).toBeEnabled();
    await expectNoAction(/^(Set|Replace|Remove) key for /);
  });

  it("offers no actions to a user who can neither connect nor manage", () => {
    renderRows(
      [
        withAccount(
          { connected: true, can_connect: false },
          { is_shared: true, can_manage: false, active_source: "oauth" },
        ),
      ],
      { agentName: "shared_dev" },
    );
    expect(
      within(row("GitHub")).getByText("Using connected account"),
    ).toBeVisible();
    expect(
      within(row("GitHub")).getByText(
        "Shared with everyone using Personal Mind",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows the connected account with Disconnect", async () => {
    renderRows([connectedGithub]);
    const github = row("GitHub");
    expect(within(github).getByText("Using connected account")).toBeVisible();
    expect(within(github).getByText("octocat")).toBeVisible();
    expect(screen.queryByText(/shared with everyone/i)).toBeNull();
    expect(
      await rowAction("Disconnect for GitHub", github),
    ).toBeInTheDocument();
    expect(
      within(github).queryByRole("button", { name: /Connect/ }),
    ).toBeNull();
  });

  it("says the account is in use when the provider gives no account label", () => {
    renderRows([
      withAccount(
        { connected: true },
        { configured: true, active_source: "oauth" },
      ),
    ]);
    expect(
      within(row("GitHub")).getByText("Using connected account"),
    ).toBeInTheDocument();
  });

  it("disconnects only after confirmation and reports the change", async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      json({ status: "disconnected", provider: "github" }),
    );
    const onChanged = vi.fn();
    renderRows([connectedGithub], { onChanged });
    fireEvent.click(await rowAction("Disconnect for GitHub"));
    expect(fetch).not.toHaveBeenCalled();
    await pressInDialog("Disconnect");

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/egress/agents/personal/github/disconnect",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
  });

  it("does not disconnect when the confirmation is cancelled", async () => {
    renderRows([connectedGithub]);
    fireEvent.click(await rowAction("Disconnect for GitHub"));
    await pressInDialog("Cancel");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("says the API key is in use and keeps its controls visible", async () => {
    renderRows([
      withAccount(
        { connected: true, account_label: "octocat" },
        { configured: true, active_source: "key", key_configured: true },
      ),
    ]);
    const github = row("GitHub");
    expect(within(github).getByText("Using API key")).toBeInTheDocument();
    // The account stays connected, but the row reports the key it uses.
    expect(within(github).queryByText("Connected")).toBeNull();
    expect(within(github).queryByText("octocat")).toBeNull();
    expect(
      within(github).getByText(
        "Your connected GitHub account is kept for other services.",
      ),
    ).toBeVisible();
    expect(
      await rowAction("Replace key for GitHub", github),
    ).toBeInTheDocument();
    expect(
      await rowAction("Remove key for GitHub", github),
    ).toBeInTheDocument();
    await expectNoAction("Use an API key instead for GitHub", github);
  });

  it("offers only Reset connection while the saved connection needs a reset", () => {
    renderRows([withAccount({ reset_required: true })]);
    const github = row("GitHub");
    expect(within(github).getByText("Reset required")).toBeInTheDocument();
    expect(
      within(github).getByRole("button", {
        name: "Reset connection for GitHub",
      }),
    ).toBeEnabled();
    // Connecting reads the unreadable connection first, so it cannot work until the reset.
    expect(
      within(github).queryByRole("button", { name: /Connect/ }),
    ).toBeNull();
  });

  it("lets a user without key rights disconnect their requester-scoped account", async () => {
    renderRows(
      [
        withAccount(
          {
            connected: true,
            account_label: "octocat",
            shared_worker_opt_in: true,
          },
          {
            is_shared: true,
            can_manage: false,
            configured: true,
            active_source: "oauth",
          },
        ),
      ],
      { agentName: "shared_dev" },
    );
    expect(await rowAction("Disconnect for GitHub")).not.toHaveAttribute(
      "aria-disabled",
    );
    await expectNoAction(/^(Set|Replace|Remove) key for /);
  });

  it("explains that personal accounts cannot be used by a shared agent", async () => {
    renderRows(
      [
        withAccount(
          { can_connect: false, unavailable_reason: "shared_sandbox" },
          { is_shared: true },
        ),
      ],
      { agentName: "shared_dev" },
    );
    const github = row("GitHub");
    expect(
      within(github).getByText(
        "Personal GitHub accounts can't be used by shared agents. Add a team API key instead.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/can't be used in this sandbox/)).toBeNull();
    expect(
      within(github).queryByRole("button", { name: /Connect/ }),
    ).toBeNull();
    expect(
      within(github).getByRole("button", { name: "Set key for GitHub" }),
    ).toBeEnabled();
  });

  it("warns that everyone using a shared agent can act with an opted-in account", () => {
    renderRows(
      [withAccount({ shared_worker_opt_in: true }, { is_shared: true })],
      { agentName: "shared_dev" },
    );
    const github = row("GitHub");
    expect(
      within(github).getByText(
        "Everyone using this agent can act with the connected account until its access expires",
      ),
    ).toBeVisible();
    expect(
      within(github).getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    ).toBeEnabled();
  });

  it("resets an unreadable connection after confirmation", async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      json({ status: "disconnected", provider: "github" }),
    );
    const onChanged = vi.fn();
    renderRows([withAccount({ reset_required: true })], { onChanged });
    fireEvent.click(
      screen.getByRole("button", { name: "Reset connection for GitHub" }),
    );
    expect(fetch).not.toHaveBeenCalled();
    await pressInDialog("Reset connection");
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "/api/connections/egress/agents/personal/github/disconnect",
    );
  });

  it("stops waiting for authorization when Cancel is clicked", async () => {
    vi.mocked(fetch).mockImplementation(async () => json(authorization));
    const onChanged = await startConnect();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      }),
    ).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    await waitFor(() => expect(popup.close).toHaveBeenCalled());
    expect(onChanged).not.toHaveBeenCalled();
  });

  describe("while Agent Keys may not use the account", () => {
    const toolsOnly = withAccount(
      { connected: true, account_label: "octocat", reuse_enabled: false },
      { configured: false, active_source: null },
    );
    const approvalBypass =
      "Commands this agent runs (shell, python) will act as octocat with its full access, without the approval prompts GitHub tools ask for.";

    it("says the account serves its tools only and offers to use it after confirming the approval bypass", async () => {
      const onChanged = vi.fn();
      renderRows([toolsOnly], { onChanged });
      const github = row("GitHub");
      expect(
        within(github).getByText("Connected for tools only"),
      ).toBeVisible();
      expect(within(github).getByText("octocat")).toBeVisible();
      const use = within(github).getByRole("button", {
        name: "Use account for GitHub",
      });
      expect(use).toHaveTextContent("Use account");

      fireEvent.click(use);
      const dialog = await screen.findByRole("dialog");
      expect(dialog).toHaveTextContent(
        "Use your GitHub account for Agent Keys?",
      );
      expect(dialog).toHaveTextContent(
        `${approvalBypass} This covers each of your agents whose GitHub service uses this account.`,
      );
      expect(fetch).not.toHaveBeenCalled();
      await pressInDialog("Use account");

      await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
      expect(fetch).toHaveBeenCalledWith(
        "/api/connections/egress/agents/personal/github/oauth-reuse",
        expect.objectContaining({
          method: "PUT",
          credentials: "same-origin",
          body: JSON.stringify({ acknowledge_approval_bypass: true }),
        }),
      );
    });

    it("leaves the account alone when the confirmation is cancelled", async () => {
      renderRows([toolsOnly]);
      fireEvent.click(
        screen.getByRole("button", {
          name: "Use account for GitHub",
        }),
      );
      await pressInDialog("Cancel");
      expect(fetch).not.toHaveBeenCalled();
    });

    it("connects for Agent Keys by allowing the account, then running the sign-in", async () => {
      vi.mocked(fetch).mockImplementation(async (input) =>
        String(input).endsWith("/oauth-reuse")
          ? noContent()
          : json(authorization),
      );
      const onChanged = vi.fn();
      renderRows([withAccount({ reuse_enabled: false })], { onChanged });
      const connect = screen.getByRole("button", {
        name: "Connect GitHub for Agent Keys for GitHub",
      });
      expect(connect).toHaveTextContent("Connect");

      fireEvent.click(connect);
      expect(await screen.findByRole("dialog")).toHaveTextContent(
        "will act as the GitHub account you connect with its full access",
      );
      expect(window.open).not.toHaveBeenCalled();
      await pressInDialog("Continue");

      await waitFor(() =>
        expect(popup.location.href).toBe(authorization.auth_url),
      );
      expect(vi.mocked(fetch).mock.calls.map(([path]) => path)).toEqual([
        "/api/connections/egress/agents/personal/github/oauth-reuse",
        "/api/connections/egress/agents/personal/github/connect",
      ]);
      answer("connected");
      await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    });

    it("tells everyone using a shared agent that they get its account", async () => {
      renderRows(
        [
          withAccount(
            {
              provider: "google_drive",
              display_name: "Google Drive",
              connected: true,
              account_label: "team@example.com",
              reuse_enabled: false,
            },
            {
              name: "drive",
              display_name: "Drive",
              is_shared: true,
              active_source: null,
            },
          ),
        ],
        { agentName: "shared_dev" },
      );
      fireEvent.click(
        screen.getByRole("button", {
          name: "Use account for Drive",
        }),
      );
      expect(await screen.findByRole("dialog")).toHaveTextContent(
        "without the approval prompts Google Drive tools ask for. Everyone using Personal Mind gets this.",
      );
    });

    it("tells a manager of agents without a scope that every one of them gets the account", async () => {
      const unscoped = withAccount(
        {
          provider: "google_drive",
          display_name: "Google Drive",
          connected: true,
          account_label: "team@example.com",
          reuse_enabled: false,
        },
        { name: "drive", display_name: "Drive", is_shared: true },
      );
      renderRows([unscoped], {
        agent: {
          displayName: "the agents without a scope",
          shared: true,
          scope: "unscoped",
          services: [unscoped],
        },
      });
      fireEvent.click(
        screen.getByRole("button", {
          name: "Use account for Drive",
        }),
      );
      const dialog = await screen.findByRole("dialog");
      expect(dialog).toHaveTextContent(
        "without the approval prompts Google Drive tools ask for. Every agent without a scope gets this.",
      );
      expect(dialog).not.toHaveTextContent("Everyone using");
    });

    it("offers nothing to a user who may not change the account", () => {
      renderRows(
        [
          withAccount(
            { connected: true, can_connect: false, reuse_enabled: false },
            { is_shared: true, can_manage: false },
          ),
        ],
        { agentName: "shared_dev" },
      );
      expect(
        within(row("GitHub")).getByText("Connected for tools only"),
      ).toBeVisible();
      expect(screen.queryByRole("button")).toBeNull();
    });
  });

  it("stops Agent Keys using the account from the menu and keeps the connection", async () => {
    const onChanged = vi.fn();
    renderRows([connectedGithub], { onChanged });
    expect(
      within(row("GitHub")).getByText("Using connected account"),
    ).toBeVisible();
    fireEvent.click(await rowAction("Stop using GitHub account for GitHub"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/egress/agents/personal/github/oauth-reuse",
      expect.objectContaining({ method: "DELETE", credentials: "same-origin" }),
    );
  });

  it("says Disconnect removes the connection for the agent's tools too", async () => {
    renderRows([connectedGithub]);
    fireEvent.click(await rowAction("Disconnect for GitHub"));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "GitHub tools lose it too. To keep it for them, choose Stop using GitHub account instead.",
    );
  });

  it("says Agent Keys cannot use a shared service account and offers no connect action", () => {
    // The responses for Google Drive while GOOGLE_SERVICE_ACCOUNT_FILE is set.
    const drive = (
      account: Partial<EgressOAuthStatus>,
      service: Partial<EgressCredentialService>,
    ) =>
      credentialService({
        name: "drive",
        display_name: "Google Drive",
        oauth: oauthStatus({
          provider: "google_drive",
          display_name: "Google Drive",
          can_connect: false,
          service_account: true,
          ...account,
        }),
        ...service,
      });
    renderRows([drive({}, {})]);
    expect(
      within(row("Google Drive")).getByText("Not set"),
    ).toBeInTheDocument();
    expect(
      within(row("Google Drive")).getByText(
        "Agent Keys cannot use the shared service account, so this service needs a key.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Connect/ })).toBeNull();
    cleanup();

    renderRows([
      drive({ connected: true }, { configured: true, active_source: "oauth" }),
    ]);
    expect(
      within(row("Google Drive")).getByText(
        "Agent Keys cannot use the shared service account, so this uses the account connected earlier.",
      ),
    ).toBeInTheDocument();
  });

  it("leaves services without an account provider as plain key rows", async () => {
    renderRows([github]);
    expect(screen.queryByRole("button", { name: /Connect/ })).toBeNull();
    await expectNoAction(/Use an API key instead/);
    expect(
      screen.getByRole("button", { name: "Set key for GitHub" }),
    ).toBeInTheDocument();
  });
});

describe("egress service rows with service editing", () => {
  const mine: EgressCredentialService = {
    ...github,
    name: "mine",
    display_name: "Mine",
    source: "user",
  };
  const theirs: EgressCredentialService = {
    ...github,
    name: "theirs",
    display_name: "Theirs",
    source: "config",
  };

  it("offers Edit and Delete for the editable source and labels the rest", async () => {
    renderRows([mine, theirs], {
      serviceEditing: editing({
        labels: { config: "Added by your administrator" },
      }),
    });
    expect(
      await rowAction("Edit service for Mine", row("Mine")),
    ).toBeInTheDocument();
    expect(
      await rowAction("Delete service for Mine", row("Mine")),
    ).toBeInTheDocument();
    expect(
      within(row("Mine")).queryByText("Added by your administrator"),
    ).toBeNull();
    await expectNoAction(/Edit|Delete/, row("Theirs"));
    expect(
      within(row("Theirs")).getByText("Added by your administrator"),
    ).toBeInTheDocument();
  });

  it("hides Edit and Delete from someone who cannot manage the service", async () => {
    renderRows([{ ...mine, can_manage: false }], { serviceEditing: editing() });
    await expectNoAction(/Edit|Delete/);
  });

  it("turns Edit and Delete off while something else is being written", async () => {
    renderRows([mine], { serviceEditing: editing({ disabled: true }) });
    expect(await rowAction("Edit service for Mine")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(await rowAction("Delete service for Mine")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    // The key controls are not part of a service write.
    expect(
      screen.getByRole("button", { name: "Set key for Mine" }),
    ).toBeEnabled();
  });

  it("hands the service to the Edit callback", async () => {
    const serviceEditing = editing();
    renderRows([mine], { serviceEditing });
    fireEvent.click(await rowAction("Edit service for Mine"));
    expect(serviceEditing.onEdit).toHaveBeenCalledWith(mine);
  });

  it("deletes only after the warning is confirmed", async () => {
    const serviceEditing = editing({
      deleteWarning: (service) => `Deleting ${service.name} cannot be undone.`,
    });
    renderRows([mine], { serviceEditing });
    fireEvent.click(await rowAction("Delete service for Mine"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Deleting mine cannot be undone.");
    expect(serviceEditing.onDelete).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(serviceEditing.onDelete).not.toHaveBeenCalled();

    fireEvent.click(await rowAction("Delete service for Mine"));
    await pressInDialog("Delete");
    await waitFor(() =>
      expect(serviceEditing.onDelete).toHaveBeenCalledWith(mine),
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("shows why a delete failed and keeps the row", async () => {
    const serviceEditing = editing({
      onDelete: vi.fn(async () => {
        throw new Error("Service is not a service of your own");
      }),
    });
    renderRows([mine], { serviceEditing });
    fireEvent.click(await rowAction("Delete service for Mine"));
    await pressInDialog("Delete");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Service is not a service of your own",
    );
    expect(await rowAction("Delete service for Mine")).not.toHaveAttribute(
      "aria-disabled",
    );
  });
});

describe("rows with an agent's wording", () => {
  const agent = (overrides: Partial<RowAgent> = {}): RowAgent => ({
    displayName: "Personal Mind",
    shared: (overrides.services ?? []).some(
      (service) => service.is_shared === true,
    ),
    managers: [],
    services: [],
    ...overrides,
  });
  const keyed = (
    overrides: Partial<EgressCredentialService> = {},
  ): EgressCredentialService => ({
    ...openai,
    key_updated_at: "2026-10-05T12:00:00+00:00",
    ...overrides,
  });

  it("says Key saved with the date, and Set key, Replace key and Remove key on the buttons", async () => {
    renderRows([github, keyed()]);
    const date = new Date("2026-10-05T12:00:00+00:00").toLocaleDateString();
    expect(
      within(row("OpenAI")).getByText(`Key saved ${date}`),
    ).toBeInTheDocument();
    expect(within(row("GitHub")).getByText("Not set")).toBeInTheDocument();
    expect(
      within(row("GitHub")).getByRole("button", { name: "Set key for GitHub" }),
    ).toHaveTextContent(/^Set key$/);
    expect(
      await rowAction("Replace key for OpenAI", row("OpenAI")),
    ).toHaveTextContent(/^Replace key$/);
    expect(
      await rowAction("Remove key for OpenAI", row("OpenAI")),
    ).toHaveTextContent(/^Remove key$/);
  });

  it("adds no kept-account note when no account is connected or the account is the one in use", () => {
    renderRows([
      withAccount(
        { connected: false },
        { configured: true, active_source: "key", key_configured: true },
      ),
    ]);
    expect(screen.queryByText(/is kept for other services/)).toBeNull();
    cleanup();
    renderRows([connectedGithub]);
    expect(screen.queryByText(/is kept for other services/)).toBeNull();
  });

  it("shows a shared account to everyone as connected and shared with everyone using the agent", () => {
    const shared = withAccount(
      { connected: true, account_label: "team@example.org" },
      {
        is_shared: true,
        can_manage: false,
        configured: true,
        active_source: "oauth",
      },
    );
    renderRows([shared], {
      agent: agent({ displayName: "Shared Dev", services: [shared] }),
    });
    const status = within(row("GitHub"));
    expect(status.getByText("Using connected account")).toBeVisible();
    expect(status.getByText("team@example.org")).toBeVisible();
    expect(
      status.getByText("Shared with everyone using Shared Dev"),
    ).toBeVisible();
  });

  describe("Connect on a shared agent", () => {
    const sharedGoogle = withAccount(
      { provider: "google", display_name: "Google" },
      { is_shared: true, name: "gmail", display_name: "Gmail" },
    );
    beforeEach(() => {
      fakePopup();
      vi.mocked(fetch).mockImplementation(async () =>
        json({
          provider: "google",
          auth_url: "https://accounts.google.com/o/oauth2/v2/auth",
          completion_origin: "https://portal.example.com",
        }),
      );
    });

    it("asks Connect a shared account? and only goes on after Continue", async () => {
      renderRows([sharedGoogle], {
        agent: agent({ displayName: "Shared Dev", services: [sharedGoogle] }),
      });
      fireEvent.click(
        screen.getByRole("button", {
          name: "Connect Google for Agent Keys for Gmail",
        }),
      );

      const dialog = await screen.findByRole("dialog", {
        name: "Connect a shared account?",
      });
      expect(
        within(dialog).getByText(
          "Everyone who uses Shared Dev will act as the Google account you pick. Use a team account, not your personal one.",
        ),
      ).toBeVisible();
      expect(fetch).not.toHaveBeenCalled();

      fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
      await waitFor(() =>
        expect(fetch).toHaveBeenCalledWith(
          "/api/connections/egress/agents/personal/gmail/connect",
          expect.objectContaining({ method: "POST" }),
        ),
      );
      expect(
        screen.queryByRole("dialog", { name: "Connect a shared account?" }),
      ).toBeNull();
    });

    it("sends nothing when the question is cancelled", async () => {
      renderRows([sharedGoogle], {
        agent: agent({ displayName: "Shared Dev", services: [sharedGoogle] }),
      });
      fireEvent.click(
        screen.getByRole("button", {
          name: "Connect Google for Agent Keys for Gmail",
        }),
      );
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(fetch).not.toHaveBeenCalled();
      expect(window.open).not.toHaveBeenCalled();
    });

    it("does not ask for a requester's own account that a shared sandbox uses by opt-in", async () => {
      const optedIn = withAccount(
        { shared_worker_opt_in: true },
        { is_shared: true },
      );
      renderRows([optedIn], { agent: agent({ services: [optedIn] }) });
      fireEvent.click(
        screen.getByRole("button", {
          name: "Connect GitHub for Agent Keys for GitHub",
        }),
      );
      await waitFor(() => expect(fetch).toHaveBeenCalled());
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("does not call that account shared with everyone either", () => {
      const optedIn = withAccount(
        {
          connected: true,
          account_label: "octocat",
          shared_worker_opt_in: true,
        },
        { is_shared: true, configured: true, active_source: "oauth" },
      );
      renderRows([optedIn], { agent: agent({ services: [optedIn] }) });
      expect(within(row("GitHub")).getByText("octocat")).toBeVisible();
      expect(screen.queryByText(/shared with everyone/i)).toBeNull();
    });

    it("does not ask on a personal agent", async () => {
      const personal = { ...sharedGoogle, is_shared: false };
      renderRows([personal], { agent: agent({ services: [personal] }) });
      fireEvent.click(
        screen.getByRole("button", {
          name: "Connect Google for Agent Keys for Gmail",
        }),
      );
      await waitFor(() => expect(fetch).toHaveBeenCalled());
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  describe("a personal account the shared agent refuses", () => {
    const refused = withAccount(
      { can_connect: false, unavailable_reason: "shared_sandbox" },
      { is_shared: true, can_manage: false },
    );

    it("tells a non-manager whom to ask", () => {
      renderRows([refused], {
        agent: agent({
          managers: ["@bob:example.org", "@carol:example.org"],
          services: [refused],
        }),
      });
      expect(
        within(row("GitHub")).getByText(
          "Personal GitHub accounts can't be used by shared agents. Ask @bob:example.org and @carol:example.org to add a team API key.",
        ),
      ).toBeVisible();
      expect(within(row("GitHub")).getByText("Not set")).toBeVisible();
    });

    it("tells a non-manager to ask an administrator when there are no managers", () => {
      renderRows([refused], {
        agent: agent({ managers: [], services: [refused] }),
      });
      expect(
        within(row("GitHub")).getByText(
          /Ask an administrator to add a team API key\.$/,
        ),
      ).toBeVisible();
    });

    it("tells a manager to add a team key", () => {
      const asManager = { ...refused, can_manage: true };
      renderRows([asManager], {
        agent: agent({ managers: ["@bob:example.org"], services: [asManager] }),
      });
      expect(
        within(row("GitHub")).getByText(
          "Personal GitHub accounts can't be used by shared agents. Add a team API key instead.",
        ),
      ).toBeVisible();
    });

    it("drops the hint and says Key saved with the date once a team key is saved", () => {
      const keyed = {
        ...refused,
        can_manage: true,
        configured: true,
        active_source: "key" as const,
        key_configured: true,
        key_updated_at: "2026-10-05T12:00:00+00:00",
      };
      renderRows([keyed], {
        agent: agent({ managers: ["@alice:example.org"], services: [keyed] }),
      });
      expect(screen.queryByText(/can't be used by shared agents/)).toBeNull();
      expect(
        within(row("GitHub")).getByText(
          `Key saved ${new Date("2026-10-05T12:00:00+00:00").toLocaleDateString()}`,
        ),
      ).toBeVisible();
      expect(screen.queryByText("Using API key")).toBeNull();
    });

    it("drops the hint for someone who cannot change the key too", () => {
      const keyed = {
        ...refused,
        configured: true,
        active_source: "key" as const,
        key_configured: true,
        key_updated_at: "2026-10-05T12:00:00+00:00",
      };
      renderRows([keyed], {
        agent: agent({ managers: ["@alice:example.org"], services: [keyed] }),
      });
      expect(screen.queryByText(/can't be used by shared agents/)).toBeNull();
    });
  });

  describe("a personal account a personal agent's shared sandbox refuses", () => {
    const refusedForPersonal = withAccount(
      { can_connect: false, unavailable_reason: "shared_sandbox" },
      { is_shared: false, can_manage: true },
    );
    const SANDBOX_TEXT =
      "Personal GitHub accounts can't be used in this sandbox, which other users' commands share. Add an API key instead.";

    it("says the sandbox is shared with other users, not that the agent is shared", async () => {
      renderRows([refusedForPersonal], {
        agent: agent({ shared: false, services: [refusedForPersonal] }),
      });
      const github = row("GitHub");
      expect(within(github).getByText(SANDBOX_TEXT)).toBeVisible();
      expect(within(github).queryByText(/shared agents/)).toBeNull();
      expect(
        within(github).queryByRole("button", { name: /Connect/ }),
      ).toBeNull();
      expect(
        within(github).getByRole("button", { name: "Set key for GitHub" }),
      ).toBeEnabled();
    });

    it("does not name managers or say to ask anyone", () => {
      renderRows([refusedForPersonal], {
        agent: agent({
          shared: false,
          managers: ["@bob:example.org"],
          services: [refusedForPersonal],
        }),
      });
      expect(screen.queryByText(/@bob:example.org/)).toBeNull();
      expect(screen.queryByText(/Ask /)).toBeNull();
    });
  });

  describe("disconnecting an account", () => {
    const listing = [
      connectedGithub,
      {
        ...connectedGithub,
        name: "github-limited",
        display_name: "GitHub (a/b)",
        source: "user" as const,
      },
      openai,
      withAccount(
        { provider: "google", display_name: "Google", connected: true },
        { name: "gmail", display_name: "Gmail" },
      ),
    ];

    it("lists every service of the agent that uses the provider", async () => {
      vi.mocked(fetch).mockImplementation(async () =>
        json({ status: "disconnected", provider: "github" }),
      );
      const onChanged = vi.fn();
      renderRows(listing, { onChanged, agent: agent({ services: listing }) });
      fireEvent.click(await rowAction("Disconnect for GitHub", row("GitHub")));
      const dialog = await screen.findByRole("dialog", {
        name: "Disconnect GitHub?",
      });
      expect(
        within(dialog).getByText(
          "This removes the saved GitHub connection for Personal Mind. These services stop working until an account is connected again: GitHub and GitHub (a/b). GitHub tools lose it too. To keep it for them, choose Stop using GitHub account instead.",
        ),
      ).toBeVisible();
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Disconnect" }),
      );
      await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    });
  });

  describe("limiting a GitHub service to repositories", () => {
    const base = (overrides: Partial<EgressCredentialService> = {}) => ({
      ...github,
      source: "config" as const,
      rules: [
        { host: "api.github.com", port: null, path_prefix: "/" },
        { host: "github.com", port: null, path_prefix: "/" },
      ],
      ...overrides,
    });

    it("offers it on an administrator's GitHub row to someone who can manage the agent", async () => {
      const onLimitToRepositories = vi.fn();
      const service = base();
      renderRows([service], {
        serviceEditing: editing({ onLimitToRepositories }),
      });
      const button = await rowAction(
        "Limit to repositories for GitHub",
        row("GitHub"),
      );
      expect(button).toHaveTextContent("Limit to repositories");
      fireEvent.click(button);
      expect(onLimitToRepositories).toHaveBeenCalledWith(service);
    });

    it.each([
      ["the viewer cannot manage the agent", base({ can_manage: false })],
      ["the service is the viewer's own", base({ source: "user" })],
      [
        "it has no rule on a GitHub host",
        base({
          rules: [{ host: "httpbin.org", port: null, path_prefix: "/" }],
        }),
      ],
    ])("does not offer it when %s", async (_why, service) => {
      renderRows([service], {
        serviceEditing: editing({ onLimitToRepositories: vi.fn() }),
      });
      await expectNoAction(/Limit to repositories/);
    });

    it("does not offer it without a handler, as on the dashboard", async () => {
      renderRows([base()], {
        serviceEditing: editing(),
      });
      await expectNoAction(/Limit to repositories/);
    });

    it("turns it off while the editor is open or something is being written", async () => {
      renderRows([base()], {
        serviceEditing: editing({
          onLimitToRepositories: vi.fn(),
          limitDisabled: true,
        }),
      });
      expect(
        await rowAction("Limit to repositories for GitHub"),
      ).toHaveAttribute("aria-disabled", "true");
      cleanup();
      renderRows([base()], {
        serviceEditing: editing({
          onLimitToRepositories: vi.fn(),
          disabled: true,
        }),
      });
      expect(
        await rowAction("Limit to repositories for GitHub"),
      ).toHaveAttribute("aria-disabled", "true");
    });

    it("shows which repositories a limited service is limited to", () => {
      const rules = ruleSummaries(
        githubRepositoryRules("a/b\nc/*", { uploads: false, graphql: false })
          .rules,
      );
      renderRows([base({ source: "user", display_name: "Mine", rules })]);
      expect(
        within(row("Mine")).getByText("Limited to a/b, c/*"),
      ).toBeVisible();
    });

    it("shows nothing for a service that reaches all of GitHub", () => {
      renderRows([base()]);
      expect(screen.queryByText(/Limited to/)).toBeNull();
    });

    it("does not repeat a description that already says it", () => {
      const rules = ruleSummaries(
        githubRepositoryRules("a/b", { uploads: false, graphql: false }).rules,
      );
      renderRows([
        base({
          source: "user",
          display_name: "Mine",
          description: "Limited to a/b",
          rules,
        }),
      ]);
      expect(screen.getAllByText("Limited to a/b")).toHaveLength(1);
    });
  });

  it("gives every key and limit button an accessible name that starts with its visible text", async () => {
    const limitable = {
      ...github,
      name: "gh",
      display_name: "GitHub",
      source: "config" as const,
      rules: [{ host: "api.github.com", port: null, path_prefix: "/" }],
    };
    const keyed = { ...openai, name: "openai", display_name: "OpenAI" };
    renderRows([limitable, keyed], {
      serviceEditing: editing({ onLimitToRepositories: vi.fn() }),
    });
    const labels = [
      "Set key",
      "Replace key",
      "Remove key",
      "Limit to repositories",
    ];
    const controls = [
      ...(await rowControls("GitHub")),
      ...(await rowControls("OpenAI")),
    ].filter(({ text }) => labels.includes(text));
    expect(controls.map(({ text }) => text)).toEqual([
      "Set key",
      "Limit to repositories",
      "Replace key",
      "Remove key",
    ]);
    for (const { text, name } of controls)
      expect(name).toMatch(new RegExp(`^${text} for (GitHub|OpenAI)$`));
  });

  it("names every account and service button after its service, so two GitHub rows never share a name", async () => {
    const mineOwn = {
      source: "user" as const,
      configured: true,
      active_source: "oauth" as const,
    };
    const connected = withAccount(
      { connected: true, account_label: "octocat" },
      { ...mineOwn, name: "github", display_name: "GitHub" },
    );
    const needsReset = withAccount(
      { reset_required: true },
      {
        ...mineOwn,
        name: "work",
        display_name: "Work GitHub",
        configured: false,
        active_source: null,
      },
    );
    renderRows([connected, needsReset], {
      serviceEditing: editing(),
    });
    const visible = [
      "Disconnect",
      "Reset connection",
      "Edit service",
      "Delete service",
    ];
    const controls = [
      ...(await rowControls("GitHub")),
      ...(await rowControls("Work GitHub")),
    ].filter(({ text }) => visible.includes(text));
    // A row's main action comes first, then its menu, which ends with what removes something.
    expect(controls.map(({ name }) => name)).toEqual([
      "Edit service for GitHub",
      "Disconnect for GitHub",
      "Delete service for GitHub",
      "Reset connection for Work GitHub",
      "Edit service for Work GitHub",
      "Delete service for Work GitHub",
    ]);
    for (const { text, name } of controls)
      expect(name).toMatch(new RegExp(`^${text} for `));
    const names = controls.map(({ name }) => name);
    expect(new Set(names).size).toBe(names.length);
  });

  it("gives two rows that share a provider different Connect and API key names", async () => {
    const first = withAccount({}, { name: "github", display_name: "GitHub" });
    const second = withAccount(
      {},
      { name: "work", display_name: "Work GitHub" },
    );
    renderRows([first, second]);
    const visible = ["Connect", "Use an API key instead"];
    const controls = [
      ...(await rowControls("GitHub")),
      ...(await rowControls("Work GitHub")),
    ].filter(({ text }) => visible.includes(text));
    expect(controls.map(({ text }) => text)).toEqual([
      "Connect",
      "Use an API key instead",
      "Connect",
      "Use an API key instead",
    ]);
    expect(controls.map(({ name }) => name)).toEqual([
      "Connect GitHub for Agent Keys for GitHub",
      "Use an API key instead for GitHub",
      "Connect GitHub for Agent Keys for Work GitHub",
      "Use an API key instead for Work GitHub",
    ]);
    for (const { text, name } of controls)
      expect(name).toMatch(new RegExp(`^${text} `));
    const names = controls.map(({ name }) => name);
    expect(new Set(names).size).toBe(names.length);
    // Each name reaches its own row's button.
    expect(
      within(row("Work GitHub")).getByRole("button", {
        name: "Connect GitHub for Agent Keys for Work GitHub",
      }),
    ).toBeEnabled();
  });

  describe("the hosts a service sends to", () => {
    const hostRule = (
      host: string,
      path_prefix = "/",
      port: number | null = null,
    ) => ({
      host,
      port,
      path_prefix,
    });

    it("lists each host once, in rule order, with ports as host:port", () => {
      renderRows([
        {
          ...github,
          rules: [
            hostRule("api.github.com"),
            hostRule("github.com"),
            hostRule("api.github.com", "/graphql"),
            hostRule("localhost", "/", 8443),
            hostRule("localhost", "/v2/", 8443),
          ],
        },
      ]);
      expect(
        within(row("GitHub")).getByText(
          "api.github.com, github.com, localhost:8443",
        ),
      ).toBeVisible();
    });

    it("treats a host on another port as another host", () => {
      renderRows([
        {
          ...github,
          rules: [
            hostRule("a.example.com"),
            hostRule("a.example.com", "/", 8080),
          ],
        },
      ]);
      expect(
        within(row("GitHub")).getByText("a.example.com, a.example.com:8080"),
      ).toBeVisible();
    });

    it("shows a dash for a listing without rules", () => {
      renderRows([github, { ...openai, rules: [] }]);
      for (const name of ["GitHub", "OpenAI"])
        expect(within(row(name)).getByText("—")).toBeVisible();
    });

    it("shows it on every row of a shared agent too", () => {
      const shared = {
        ...openai,
        is_shared: true,
        rules: [hostRule("api.openai.com")],
      };
      renderRows([shared], {
        agent: agent({ shared: true, services: [shared] }),
      });
      expect(within(row("OpenAI")).getByText("api.openai.com")).toBeVisible();
    });
  });

  describe("rows no agent uses", () => {
    const connectable = withAccount({}, { source: "user" as const });
    const keyedConnected = withAccount(
      { connected: true, account_label: "octocat" },
      {
        name: "gh2",
        display_name: "GitHub 2",
        configured: true,
        active_source: "oauth",
        source: "user" as const,
      },
    );

    it("leave out every way to set, remove, or connect a key or account", async () => {
      const services = [connectable, keyedConnected, openai];
      renderRows(services, { hideKeyActions: true });
      await expectNoAction(
        /^(Set|Replace|Remove) key for |^Connect |^Disconnect |^Use an API key/,
      );
      // The status still reads.
      expect(within(row("OpenAI")).getByText("Key saved")).toBeVisible();
    });

    it("keep what changes the service itself", async () => {
      renderRows([{ ...openai, source: "user" }], {
        serviceEditing: editing(),
        hideKeyActions: true,
      });
      expect(await rowAction("Edit service for OpenAI")).not.toHaveAttribute(
        "aria-disabled",
      );
      expect(await rowAction("Delete service for OpenAI")).not.toHaveAttribute(
        "aria-disabled",
      );
      await expectNoAction(/key for /);
    });

    it("also leave out Reset connection", () => {
      renderRows([withAccount({ reset_required: true })], {
        hideKeyActions: true,
      });
      expect(screen.queryByRole("button", { name: /Reset/ })).toBeNull();
      expect(within(row("GitHub")).getByText("Reset required")).toBeVisible();
    });

    it("keep every key action by default", async () => {
      renderRows([connectable, openai]);
      expect(
        screen.getByRole("button", {
          name: "Connect GitHub for Agent Keys for GitHub",
        }),
      ).toBeEnabled();
      expect(await rowAction("Remove key for OpenAI")).not.toHaveAttribute(
        "aria-disabled",
      );
    });
  });

  it("shows no administrator source label when no label is given", () => {
    renderRows([{ ...github, source: "config" }], {
      serviceEditing: editing(),
    });
    expect(screen.queryByText(/administrator/)).toBeNull();
  });
});

describe("what a row says about how the key is sent", () => {
  const rule = (
    host: string,
    authType: EgressRuleAuthType | null,
    authName: string | null = null,
  ) => ({
    host,
    port: null,
    path_prefix: "/",
    auth_type: authType,
    auth_name: authName,
  });
  const custom = (
    rules: EgressRuleSummary[],
    changes: Partial<EgressCredentialService> = {},
  ): EgressCredentialService => ({
    ...github,
    name: "httpbin",
    display_name: "httpbin.org",
    description: "",
    rules,
    placeholder_names: [],
    ...changes,
  });

  it.each<[string, EgressRuleSummary, string]>([
    ["bearer", rule("a.example.com", "bearer"), "Sent as a bearer token"],
    [
      "header",
      rule("a.example.com", "header", "X-Api-Key"),
      "Sent as header X-Api-Key",
    ],
    [
      "query",
      rule("a.example.com", "query", "api_key"),
      "Sent as query parameter api_key",
    ],
    ["basic", rule("a.example.com", "basic"), "Sent with basic auth"],
    [
      "template",
      rule("a.example.com", "template", "Authorization"),
      "Sent with a custom header template",
    ],
  ])("says how a %s rule sends the key", (_way, ruleSummary, text) => {
    renderRows([custom([ruleSummary])]);
    expect(within(row("httpbin.org")).getByText(wholeText(text))).toBeVisible();
  });

  it("names each way a service's rules differ once, in rule order", () => {
    renderRows([
      custom([
        rule("api.github.com", "bearer"),
        rule("uploads.github.com", "bearer"),
        rule("github.com", "basic"),
      ]),
    ]);
    expect(
      within(row("httpbin.org")).getByText(
        "Sent as a bearer token and with basic auth",
      ),
    ).toBeVisible();
  });

  it("says nothing about it when the server does not say, or the service sends no key", () => {
    renderRows([
      custom([{ host: "a.example.com", port: null, path_prefix: "/" }]),
    ]);
    expect(screen.queryByText(/^Sent /)).toBeNull();
    cleanup();
    renderRows([
      custom([rule("a.example.com", null)], { restrict_only: true }),
    ]);
    expect(screen.queryByText(/^Sent /)).toBeNull();
  });

  it("tells what the agent sees for each placeholder the service sets", () => {
    renderRows([
      custom([rule("api.github.com", "bearer")], {
        placeholder_names: ["GH_TOKEN", "GITHUB_TOKEN"],
      }),
    ]);
    expect(
      within(row("httpbin.org")).getByText(
        wholeText(
          "Your agent sees GH_TOKEN=mindroom-brokered, GITHUB_TOKEN=mindroom-brokered",
        ),
      ),
    ).toBeVisible();
    expect(screen.queryByText(/is not told this key exists/)).toBeNull();
  });

  it("says a custom service without placeholders leaves the agent not told, and which website to name", () => {
    renderRows([custom([rule("httpbin.org", "header", "X-Api-Key")])]);
    expect(
      within(row("httpbin.org")).getByText(
        wholeText(
          "Your agent is not told this key exists; mention httpbin.org in your request.",
        ),
      ),
    ).toBeVisible();
  });

  it("leaves out what the agent sees where the listing does not name the variables, or an account is the way in", () => {
    renderRows([
      custom([rule("httpbin.org", "bearer")], { placeholder_names: undefined }),
    ]);
    expect(screen.queryByText(/Your agent/)).toBeNull();
    cleanup();
    renderRows([
      custom([rule("www.googleapis.com", "bearer")], {
        oauth: { ...githubAccount, provider: "google" },
      }),
    ]);
    expect(screen.queryByText(/Your agent/)).toBeNull();
  });

  it("says that requests are refused while no key is set", () => {
    renderRows([
      custom([rule("api.github.com", "bearer"), rule("github.com", "basic")]),
    ]);
    expect(
      within(row("httpbin.org")).getByText(
        "Requests are refused until a key is set.",
      ),
    ).toBeVisible();
    cleanup();
    renderRows([custom([rule("httpbin.org", "bearer")], { configured: true })]);
    expect(screen.queryByText(/Requests are refused/)).toBeNull();
  });

  it.each<[string, Partial<EgressOAuthStatus>, string]>([
    [
      "an account to connect",
      {},
      "Requests are refused until you connect an account or set a key.",
    ],
    [
      "a connected account only its tools use",
      { connected: true, account_label: "octocat", reuse_enabled: false },
      "Requests are refused until you use this account or set a key.",
    ],
    [
      "an account the sandbox refuses",
      { unavailable_reason: "shared_sandbox" },
      "Requests are refused until a key is set.",
    ],
  ])("names %s as a way to stop refusals", (_, account, note) => {
    renderRows([
      custom([rule("api.github.com", "bearer")], {
        oauth: { ...githubAccount, ...account },
      }),
    ]);
    expect(within(row("httpbin.org")).getByText(note)).toBeVisible();
  });
});

describe("the other way to connect when an account is connected", () => {
  it("offers an API key instead of the account once the account is connected", async () => {
    renderRows([connectedGithub]);
    expect(
      await rowAction(
        "Use an API key instead of the account for GitHub",
        row("GitHub"),
      ),
    ).toHaveTextContent(/^Use an API key instead of the account$/);
  });

  it("keeps the shorter wording while no account is connected", async () => {
    renderRows([withAccount()]);
    expect(
      await rowAction("Use an API key instead for GitHub", row("GitHub")),
    ).toHaveTextContent(/^Use an API key instead$/);
  });
});

describe("a limit's own row", () => {
  const limit = (
    changes: Partial<EgressCredentialService> = {},
  ): EgressCredentialService => ({
    ...github,
    name: "github-limited",
    display_name: "GitHub (a/b)",
    description: "",
    source: "user",
    restrict_only: true,
    rules: [{ host: "api.github.com", port: null, path_prefix: "/repos/a/b" }],
    ...changes,
  });

  it("shows no empty line for a limit without a description", () => {
    renderRows([limit()]);
    const paragraphs = Array.from(
      row("GitHub (a/b)").querySelectorAll("p"),
    ).map((paragraph) => paragraph.textContent);
    expect(paragraphs).toEqual(["api.github.com"]);
  });

  it("shows the description when there is one", () => {
    renderRows([limit({ description: "Limited to a/b" })]);
    expect(
      within(row("GitHub (a/b)")).getByText("Limited to a/b"),
    ).toBeVisible();
  });
});

describe("what a limited service says it is limited to", () => {
  const rules = (graphql: boolean) =>
    ruleSummaries(
      githubRepositoryRules("a/b", { uploads: false, graphql }).rules,
    );
  const limited = (graphql: boolean): EgressCredentialService => ({
    ...github,
    name: "mine",
    display_name: "Mine",
    description: "",
    rules: rules(graphql),
  });

  it("adds that GraphQL reaches every repository the key can reach when the limit allows it", () => {
    renderRows([limited(true)]);
    expect(
      within(row("Mine")).getByText(
        "Limited to a/b (plus GraphQL, which reaches every repository the key can reach)",
      ),
    ).toBeVisible();
  });

  it("says nothing of a limit when the rules also allow other GitHub paths", () => {
    renderRows([
      {
        ...limited(false),
        rules: [
          ...rules(false),
          { host: "api.github.com", port: null, path_prefix: "/user" },
        ],
      },
    ]);
    expect(screen.queryByText(/Limited to/)).toBeNull();
  });
});

describe("a popup that closes without answering", () => {
  const listingPath = "/api/connections/egress";
  // The provider's status route is for tool rows; this agent has no tool for
  // the provider, so the route answers 404 and is never the one asked.
  const answerWith = (connected: boolean | "fails") =>
    vi.mocked(fetch).mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/connect")) return json(authorization);
      if (path === "/api/connections/agents/personal/github/status")
        return json({ detail: "Not found" }, 404);
      if (path === listingPath) {
        if (connected === "fails") return json({}, 500);
        return json({
          agents: [
            {
              agent_name: "other",
              services: [withAccount({ connected: !connected })],
            },
            {
              agent_name: "personal",
              services: [withAccount({ connected })],
            },
          ],
        });
      }
      return noContent();
    });
  const reads = (path: string) =>
    vi.mocked(fetch).mock.calls.filter(([input]) => String(input) === path);
  const connectAndClose = async () => {
    const onChanged = await startConnect();
    popup.closed = true;
    return onChanged;
  };

  beforeEach(fakePopup);

  it("counts a connection that completed before the answer was read, once the listing says so", async () => {
    answerWith(true);
    const onChanged = await connectAndClose();
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1), {
      timeout: 2000,
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(reads(listingPath)).toHaveLength(1);
    expect(
      reads("/api/connections/agents/personal/github/status"),
    ).toHaveLength(0);
  });

  it("says cancelled when the listing shows nothing was connected, after one read", async () => {
    answerWith(false);
    const onChanged = await connectAndClose();
    expect(
      await screen.findByRole("alert", {}, { timeout: 2000 }),
    ).toHaveTextContent("Authorization was cancelled.");
    expect(onChanged).not.toHaveBeenCalled();
    expect(reads(listingPath)).toHaveLength(1);
  });

  it("says cancelled when the listing cannot be read", async () => {
    answerWith("fails");
    const onChanged = await connectAndClose();
    expect(
      await screen.findByRole("alert", {}, { timeout: 2000 }),
    ).toHaveTextContent("Authorization was cancelled.");
    expect(onChanged).not.toHaveBeenCalled();
  });
});
