import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import egressPresets from "@/test/fixtures/egressPresets.json";
import {
  bearerRule,
  credentialService,
  limitRules,
  presets,
  presetsReady,
  storedLimit,
  validation,
} from "@/test/egressServiceTestKit";
import { githubRepositoryRules } from "./githubRepositoryRules";
import {
  DiscardEditsNotice,
  EgressServiceEditor,
  type EgressServiceEditorProps,
} from "./EgressServiceEditor";
import type {
  AuthoredEgressService,
  EgressCredentialService,
  EgressPresetCatalog,
} from "./types";

const providers = egressPresets.oauth_providers;

/** A loadPresets that serves the fixture's catalog, with any field changed. */
const catalog = (changes: Partial<EgressPresetCatalog> = {}) =>
  vi.fn(async () => ({
    presets,
    oauth_providers: providers,
    validation,
    ...changes,
  }));
const failingPresets = () =>
  vi.fn(async () => {
    throw new Error("nope");
  });

const baseProps = () => ({
  service: null,
  context: "personal" as const,
  loadPresets: catalog(),
  onSave: vi.fn(async () => undefined),
  onClose: vi.fn(),
});

/** Render the editor and wait until the server's presets are in the dropdown. */
async function renderEditor(props: Partial<EgressServiceEditorProps> = {}) {
  const merged = { ...baseProps(), ...props };
  render(<EgressServiceEditor {...merged} />);
  await presetsReady();
  return { onSave: merged.onSave, onClose: merged.onClose };
}

/** Open the section that holds everything but the basic fields. */
const openMoreOptions = () =>
  fireEvent.click(screen.getByRole("button", { name: "More options" }));
const renderGithub = (props: Partial<EgressServiceEditorProps> = {}) =>
  renderEditor({ service: { preset: "github" }, name: "github", ...props });
const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const save = () =>
  fireEvent.click(screen.getByRole("button", { name: "Save service" }));
const enter = (value: string) => type("GitHub repositories", value);
const apply = () =>
  fireEvent.click(
    screen.getByRole("button", { name: "Use these repositories" }),
  );

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("repository helper while saving", () => {
  it("takes no edits until the save finishes, so none are lost when it closes", async () => {
    let finish = () => {};
    const onSave = vi.fn(
      () =>
        new Promise<undefined>(
          (resolve) => (finish = () => resolve(undefined)),
        ),
    );
    await renderGithub({ onSave });
    enter("owner/*");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

    const helper = screen
      .getByLabelText("GitHub repositories")
      .closest("section") as HTMLElement;
    expect(within(helper).getByLabelText("GitHub repositories")).toBeDisabled();
    for (const checkbox of within(helper).getAllByRole("checkbox"))
      expect(checkbox).toBeDisabled();
    expect(
      within(helper).getByRole("button", { name: "Use these repositories" }),
    ).toBeDisabled();
    finish();
  });
});

describe("the editor form", () => {
  it("saves a custom service in its authored form", async () => {
    const { onSave } = await renderEditor();
    type("Name", "example");
    type("Website", "API.example.com");
    type("Send the key as", "header");
    type("Header name", "X-Api-Key");
    openMoreOptions();
    type("Display name", "Example");
    type("Path prefix", "/v1/");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("example", {
      display_name: "Example",
      rules: [
        {
          host: "api.example.com",
          path_prefix: "/v1/",
          auth: { type: "header", name: "X-Api-Key" },
        },
      ],
    });
  });

  it.each<[string, string, Record<string, string>, object]>([
    [
      "template",
      "Custom template",
      { "Header name": "Authorization", "Value template": "Token {secret}" },
      {
        type: "header",
        name: "Authorization",
        template: "Token {secret}",
      },
    ],
    [
      "query",
      "Query parameter",
      { "Parameter name": "api_key" },
      { type: "query", name: "api_key" },
    ],
    ["basic", "Basic", { Username: "bot" }, { type: "basic", username: "bot" }],
  ])(
    "sends the key as %s with its own fields",
    async (choice, _label, fields, auth) => {
      const { onSave } = await renderEditor();
      type("Name", "example");
      type("Website", "api.example.com");
      type("Send the key as", choice);
      for (const [label, value] of Object.entries(fields)) type(label, value);
      save();
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
      expect(onSave).toHaveBeenCalledWith("example", {
        display_name: "api.example.com",
        rules: [{ host: "api.example.com", auth }],
      });
    },
  );

  it("offers the five ways to send the key", async () => {
    await renderEditor();
    expect(
      within(screen.getByLabelText("Send the key as"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "Bearer token",
      "Header",
      "Basic",
      "Query parameter",
      "Custom template",
    ]);
  });

  it("says that an empty value template sends the key as is", async () => {
    await renderEditor();
    type("Send the key as", "template");
    expect(screen.getByLabelText("Value template")).toHaveAccessibleDescription(
      "Leave empty to send the key as is.",
    );
    // A query parameter has the same optional template.
    type("Send the key as", "query");
    expect(screen.getByLabelText("Value template")).toHaveAccessibleDescription(
      "Leave empty to send the key as is.",
    );
  });

  it("says that placeholders are only set while the service has a key or account", async () => {
    await renderEditor();
    openMoreOptions();
    expect(
      screen.getByText(
        /Placeholders are set only while this service has a key or a connected account\./,
      ),
    ).toBeVisible();
  });

  it("drops a custom template when the user chooses plain Header", async () => {
    const { onSave } = await renderEditor({
      name: "svc",
      service: {
        rules: [
          {
            host: "api.example.com",
            auth: { type: "header", name: "X-Key", template: "Key {secret}" },
          },
        ],
      },
    });
    expect(screen.getByLabelText("Send the key as")).toHaveValue("template");
    type("Send the key as", "header");
    expect(screen.queryByLabelText("Value template")).toBeNull();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(vi.mocked(onSave).mock.calls[0][1].rules?.[0].auth).toEqual({
      type: "header",
      name: "X-Key",
    });
  });

  it("adds, fills, and removes extra rules", async () => {
    const { onSave } = await renderEditor();
    type("Name", "two");
    type("Website", "a.example.com");
    openMoreOptions();
    // The first rule is the basic fields, so it has no Remove button.
    expect(screen.queryByRole("button", { name: "Remove Rule 1" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    type("Rule 2 host", "b.example.com");
    type("Rule 2 auth type", "basic");
    type("Rule 2 username", "bot");
    type("Rule 3 host", "c.example.com");
    type("Rule 3 path prefix", "/v1");
    type("Rule 3 port", "8443");
    fireEvent.click(screen.getByRole("button", { name: "Remove Rule 3" }));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("two", {
      display_name: "a.example.com",
      rules: [
        { host: "a.example.com", auth: { type: "bearer" } },
        { host: "b.example.com", auth: { type: "basic", username: "bot" } },
      ],
    });
  });

  it("saves a preset alone as just the preset", async () => {
    const { onSave } = await renderEditor();
    type("Name", "github");
    type("Preset", "github");
    expect(
      screen.getByText(
        "The GitHub preset covers api.github.com, uploads.github.com, github.com.",
      ),
    ).toBeInTheDocument();
    // The preset supplies the rules, so no rule fields are shown.
    expect(screen.queryByLabelText("Website")).toBeNull();
    expect(screen.queryByLabelText("Send the key as")).toBeNull();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", { preset: "github" });
  });

  it("saves a preset with restrict_to_rules without expanding it", async () => {
    const { onSave } = await renderEditor();
    type("Name", "github");
    type("Preset", "github");
    openMoreOptions();
    fireEvent.click(screen.getByLabelText("Refuse other paths on these hosts"));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", {
      preset: "github",
      restrict_to_rules: true,
    });
  });

  it("lets the user replace a preset's rules with their own", async () => {
    const { onSave } = await renderEditor();
    type("Name", "ai");
    type("Preset", "openai");
    openMoreOptions();
    fireEvent.click(
      screen.getByLabelText("Replace the preset's rules with my own"),
    );
    type("Website", "proxy.example.com");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("ai", {
      preset: "openai",
      rules: [{ host: "proxy.example.com", auth: { type: "bearer" } }],
    });
  });

  it("rejects a name that is already taken, beside the name", async () => {
    const { onSave } = await renderEditor({ takenNames: ["github"] });
    type("Name", "github");
    type("Preset", "github");
    save();
    expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(
      /A service named github already exists/,
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows the service's own error and stays open", async () => {
    const onSave = vi.fn(async () => {
      throw new Error("service name 'x' is already used by your administrator");
    });
    await renderEditor({ onSave });
    type("Name", "x");
    type("Preset", "openai");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already used by your administrator",
    );
    expect(screen.getByRole("button", { name: "Save service" })).toBeEnabled();
  });

  it("cannot rename the service it edits", async () => {
    await renderGithub();
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(screen.getByLabelText("Name")).toHaveValue("github");
    expect(screen.getByLabelText("Preset")).toHaveValue("github");
    expect(
      screen.getByRole("form", { name: "Edit github" }),
    ).toBeInTheDocument();
  });

  it("keeps a preset this page does not know", async () => {
    await renderEditor({ name: "future", service: { preset: "future_api" } });
    expect(screen.getByLabelText("Preset")).toHaveValue("future_api");
  });

  it("shows the placeholder rules as hints under More options", async () => {
    await renderEditor();
    expect(screen.queryByText(/must be TOKEN, KEY/)).toBeNull();
    openMoreOptions();
    expect(
      screen.getByText(
        /one word between underscores must be TOKEN, KEY, SECRET, PASSWORD, PAT, AUTH, or CREDENTIAL/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Use a fixed value such as mindroom-brokered/),
    ).toBeInTheDocument();
  });

  it("lists the words and prefixes a personal placeholder name cannot use", async () => {
    await renderEditor();
    openMoreOptions();
    const hint = screen.getByText(/one word between underscores must be/);
    expect(hint).toHaveTextContent(
      "No word may be COMMAND, CMD, FILE, DIR, PROVIDER, HELPER, OPTS, OPTIONS, or EXTENSIONS",
    );
    expect(hint).toHaveTextContent(
      "names cannot start with SSH_, GIT_CONFIG_, or MINDROOM_",
    );
    expect(hint).toHaveTextContent(
      "PATH, HOME, and proxy and CA variables are reserved",
    );
  });

  it("names every word, prefix, and value length the server sends in the hint", async () => {
    await renderEditor();
    openMoreOptions();
    const hint = screen.getByText(/one word between underscores must be/);
    const { placeholder } = validation;
    for (const word of [
      ...placeholder.credential_words,
      ...placeholder.refused_words,
      placeholder.refused_prefix,
      ...placeholder.reserved_prefixes,
    ])
      expect(hint).toHaveTextContent(
        new RegExp(`(?<![A-Z0-9_])${word}(?![A-Z0-9_])`),
      );
    expect(hint).toHaveTextContent("Values are 1 to 256 letters");
  });

  it("builds the hint from the rules the server sends, not from a copy", async () => {
    await renderEditor({
      loadPresets: catalog({
        validation: {
          ...validation,
          placeholder: {
            ...validation.placeholder,
            credential_words: ["SECRET", "TICKET"],
            refused_words: ["SCRIPT"],
            refused_prefix: "SUDO_",
            reserved_prefixes: ["ACME_"],
            value_pattern: "[a-z]{1,9}",
            value_max_length: 9,
          },
        },
      }),
    });
    openMoreOptions();
    const hint = screen.getByText(/one word between underscores must be/);
    expect(hint).toHaveTextContent("must be SECRET or TICKET");
    expect(hint).toHaveTextContent("No word may be SCRIPT, and");
    expect(hint).toHaveTextContent("names cannot start with SUDO_ or ACME_.");
    expect(hint).toHaveTextContent("Values are 1 to 9 letters");
    expect(hint).not.toHaveTextContent("TOKEN");
    expect(hint).not.toHaveTextContent("SSH_");
  });

  it("gives a short hint without the lists when the presets could not be loaded", async () => {
    await renderEditor({
      loadPresets: failingPresets(),
    });
    openMoreOptions();
    const hint = screen.getByText(/The server checks the rest when you save/);
    expect(hint).toHaveTextContent(
      "Names use capital letters, digits, and underscores, and must name a credential, such as MY_API_KEY.",
    );
    expect(hint).not.toHaveTextContent("TOKEN");
    expect(hint).not.toHaveTextContent("MINDROOM_");
  });

  it("refuses a placeholder the server would, and says why", async () => {
    const { onSave } = await renderEditor();
    type("Name", "svc");
    type("Website", "a.example.com");
    openMoreOptions();
    fireEvent.click(screen.getByRole("button", { name: "Add placeholder" }));
    type("Placeholder 1 name", "SSH_AUTH_SOCK");
    save();
    expect(
      screen.getByLabelText("Placeholder 1 name"),
    ).toHaveAccessibleDescription(
      "SSH_AUTH_SOCK is not allowed: tools read names with COMMAND, CMD, FILE, DIR, PROVIDER, HELPER, OPTS, OPTIONS, or EXTENSIONS, or starting with SSH_, as programs or paths.",
    );
    type("Placeholder 1 name", "my_key");
    expect(
      screen.getByLabelText("Placeholder 1 name"),
    ).toHaveAccessibleDescription(
      "my_key is not allowed as a placeholder: name a credential, such as MY_API_KEY, with TOKEN, KEY, SECRET, PASSWORD, PAT, AUTH, or CREDENTIAL as one of its words.",
    );
    type("Placeholder 1 name", "MINDROOM_API_KEY");
    expect(
      screen.getByLabelText("Placeholder 1 name"),
    ).toHaveAccessibleDescription(
      "Placeholder name MINDROOM_API_KEY starts with reserved prefix MINDROOM_",
    );
    type("Placeholder 1 name", "MY_API_KEY");
    type("Placeholder 1 value", "has space");
    expect(
      screen.getByLabelText("Placeholder 1 value"),
    ).toHaveAccessibleDescription(
      "The value of MY_API_KEY must be 1 to 256 letters, digits, or . _ : -",
    );
    expect(screen.getByLabelText("Placeholder 1 name")).not.toHaveAttribute(
      "aria-invalid",
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows the operator's placeholder rules instead on the dashboard", async () => {
    await renderEditor({ context: "operator" });
    openMoreOptions();
    expect(
      screen.getByText(/GIT_CONFIG_ or MINDROOM_ are reserved/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/must be TOKEN, KEY/)).toBeNull();
  });

  it("gives the operator a short hint without the prefixes when the presets could not be loaded", async () => {
    await renderEditor({
      context: "operator",
      loadPresets: failingPresets(),
    });
    openMoreOptions();
    expect(
      screen.getByText(/Some names, such as PATH and HOME, are reserved/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/MINDROOM_/)).toBeNull();
  });

  it("adds a placeholder with the brokered value and saves it", async () => {
    const { onSave } = await renderEditor();
    type("Name", "svc");
    type("Website", "a.example.com");
    openMoreOptions();
    fireEvent.click(screen.getByRole("button", { name: "Add placeholder" }));
    expect(screen.getByLabelText("Placeholder 1 value")).toHaveValue(
      "mindroom-brokered",
    );
    type("Placeholder 1 name", "SVC_API_KEY");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("svc", {
      display_name: "a.example.com",
      rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
      placeholder_env: { SVC_API_KEY: "mindroom-brokered" },
    });
  });

  it("opens More options when the service already uses one", async () => {
    await renderEditor({
      name: "svc",
      service: { preset: "github", placeholder_env: { GH_TOKEN: "x" } },
    });
    expect(screen.getByLabelText("Placeholder 1 name")).toHaveValue("GH_TOKEN");
    expect(
      screen.getByRole("button", { name: "More options" }),
    ).toHaveAttribute("aria-expanded", "true");
    cleanup();
    await renderEditor({
      name: "svc",
      service: { preset: "github", restrict_to_rules: true },
    });
    expect(
      screen.getByLabelText("Refuse other paths on these hosts"),
    ).toBeChecked();
    cleanup();
    await renderEditor({ name: "svc", service: { preset: "github" } });
    expect(
      screen.getByRole("button", { name: "More options" }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("disables the account login on a shared agent", async () => {
    const { onSave } = await renderEditor({ sharedTarget: true });
    openMoreOptions();
    expect(screen.getByLabelText("Account login (optional)")).toBeDisabled();
    expect(
      screen.getByText(
        "Accounts cannot be connected on a shared agent. Use an API key.",
      ),
    ).toBeInTheDocument();
    type("Name", "github");
    type("Preset", "github");
    expect(
      screen.getByText(
        /Accounts cannot be connected on a shared agent, so this service uses an API key/,
      ),
    ).toBeInTheDocument();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", {
      preset: "github",
      oauth_provider: null,
    });
  });

  it("offers the account login on a personal agent", async () => {
    const { onSave } = await renderEditor();
    type("Name", "svc");
    type("Website", "a.example.com");
    openMoreOptions();
    type("Account login (optional)", "atlassian");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("svc", {
      display_name: "a.example.com",
      rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
      oauth_provider: "atlassian",
    });
  });

  it("keeps oauth_on_shared_workers when an operator edits a service", async () => {
    const { onSave } = await renderEditor({
      context: "operator",
      name: "github",
      service: { preset: "github", oauth_on_shared_workers: true },
    });
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", {
      preset: "github",
      oauth_on_shared_workers: true,
    });
  });

  it("confirms before deleting and shows a failure", async () => {
    const onDelete = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error("Could not delete it"))
      .mockResolvedValue(undefined);
    await renderGithub({ onDelete });
    fireEvent.click(screen.getByRole("button", { name: "Delete service" }));
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    expect(
      screen.getByRole("button", { name: "Delete service" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete service" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not delete it",
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete service" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(onDelete).toHaveBeenCalledTimes(2));
  });

  it("has no delete button for a new service", async () => {
    await renderEditor({ onDelete: vi.fn() });
    expect(screen.queryByRole("button", { name: "Delete service" })).toBeNull();
  });

  it("cancels without saving", async () => {
    const { onSave, onClose } = await renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("the GitHub helper in the editor", () => {
  it("replaces the preset's rules and turns on the restriction", async () => {
    const { onSave } = await renderGithub();
    enter("basnijholt/agent-cli");
    apply();
    // The generated rules show: the first in the basic fields, the rest under More options.
    expect(screen.getByLabelText("Website")).toHaveValue("api.github.com");
    // The extra rules have a path prefix too: the first one in page order is rule 1's.
    expect(screen.getAllByLabelText("Path prefix")[0]).toHaveValue(
      "/repos/basnijholt/agent-cli",
    );
    expect(screen.getByLabelText("Rule 3 path prefix")).toHaveValue(
      "/basnijholt/agent-cli.git",
    );
    expect(screen.getByLabelText("Rule 2 auth type")).toHaveValue("basic");
    expect(screen.getByLabelText("Rule 2 username")).toHaveValue(
      "x-access-token",
    );
    expect(
      screen.getByLabelText("Refuse other paths on these hosts"),
    ).toBeChecked();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", {
      preset: "github",
      display_name: "GitHub (basnijholt/agent-cli)",
      description: "Limited to basnijholt/agent-cli",
      restrict_to_rules: true,
      rules: [
        {
          host: "api.github.com",
          path_prefix: "/repos/basnijholt/agent-cli",
          auth: { type: "bearer" },
        },
        {
          host: "github.com",
          path_prefix: "/basnijholt/agent-cli",
          auth: { type: "basic", username: "x-access-token" },
        },
        {
          host: "github.com",
          path_prefix: "/basnijholt/agent-cli.git",
          auth: { type: "basic", username: "x-access-token" },
        },
      ],
    });
  });

  it("says what applying does to the rules, in the words of the spec", async () => {
    await renderGithub();
    expect(
      screen.getByText(
        "One repository per line, as owner/repo, or owner/* for everything an owner has. Applying replaces the rules above with rules for just those repositories (API and git over HTTPS) and refuses every other path on api.github.com and github.com. Other API paths, such as /user or search, stop working. Matching is case sensitive, so write names as they appear in GitHub URLs.",
      ),
    ).toBeVisible();
  });

  describe("while another service of the viewer's opens the GitHub hosts", () => {
    const githubHosts = ["api.github.com", "github.com"].map((host) => ({
      host,
      port: null,
      path_prefix: "/",
      auth_type: "bearer" as const,
      auth_name: null,
    }));
    const own = (changes: Partial<EgressCredentialService> = {}) =>
      credentialService({
        name: "my-github",
        display_name: "My GitHub",
        source: "user",
        rules: githubHosts,
        ...changes,
      });

    it("notes that it has rules there too, instead of saying other API paths stop working", async () => {
      await renderGithub({ agentServices: [own()] });
      expect(
        screen.getByText(
          /Applying replaces the rules above with rules for just those repositories \(API and git over HTTPS\) and refuses every other path on api\.github\.com and github\.com\. My GitHub also has rules on api\.github\.com and github\.com\. A request is allowed when any of this agent's own services allows it\./,
        ),
      ).toBeVisible();
      expect(screen.queryByText(/stop working/)).toBeNull();
      expect(screen.queryByText(/not refused/)).toBeNull();
    });

    it("keeps saying they are refused for a limit, the administrator's service, and the service itself", async () => {
      await renderGithub({
        agentServices: [
          own({ name: "limit", restrict_only: true }),
          own({ name: "admin", source: "config" }),
          own({ name: "github" }),
        ],
      });
      expect(screen.getByText(/refuses every other path/)).toBeVisible();
    });
  });

  it("makes owner-wide rules and leaves uploads and GraphQL off by default", async () => {
    await renderGithub();
    expect(
      screen.getByLabelText(/Also allow uploads.github.com/),
    ).not.toBeChecked();
    expect(screen.getByLabelText(/Also allow GraphQL/)).not.toBeChecked();
    enter("mindroom-ai/*");
    apply();
    expect(screen.getAllByLabelText("Path prefix")[0]).toHaveValue(
      "/repos/mindroom-ai/",
    );
    expect(screen.getByLabelText("Rule 2 path prefix")).toHaveValue(
      "/mindroom-ai/",
    );
    expect(screen.queryByLabelText("Rule 3 host")).toBeNull();
  });

  it("adds uploads and GraphQL rules when ticked", async () => {
    await renderGithub();
    fireEvent.click(screen.getByLabelText(/Also allow uploads.github.com/));
    fireEvent.click(screen.getByLabelText(/Also allow GraphQL/));
    enter("o/r");
    apply();
    expect(screen.getByLabelText("Rule 4 host")).toHaveValue(
      "uploads.github.com",
    );
    expect(screen.getByLabelText("Rule 4 path prefix")).toHaveValue(
      "/repos/o/r",
    );
    expect(screen.getByLabelText("Rule 5 path prefix")).toHaveValue("/graphql");
  });

  it("names the lines it cannot use and changes nothing", async () => {
    await renderGithub();
    enter("not a repo");
    apply();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "not a repo is not owner/repo or owner/*",
    );
    expect(screen.queryByLabelText("Website")).toBeNull();
  });

  it("only appears for the GitHub preset or a GitHub host", async () => {
    const helper = () => screen.queryByText("Limit to repositories");
    // A new custom service has no host yet.
    await renderEditor({ name: undefined, service: null });
    expect(helper()).toBeNull();
    type("Website", "httpbin.org");
    expect(helper()).toBeNull();
    for (const host of ["github.com", "api.github.com", "uploads.github.com"]) {
      type("Website", host);
      expect(helper()).toBeInTheDocument();
    }
    type("Website", "");
    expect(helper()).toBeNull();
    type("Preset", "github");
    expect(helper()).toBeInTheDocument();
    type("Preset", "openai");
    expect(helper()).toBeNull();
    cleanup();
    await renderEditor({ name: "code", service: { rules: [bearerRule] } });
    expect(helper()).toBeNull();
  });

  it("sits with the basic fields, not under More options", async () => {
    await renderGithub();
    expect(screen.getByText("Limit to repositories")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "More options" }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("fills the display name and the description when they are empty", async () => {
    const { onSave } = await renderGithub({ name: undefined, service: null });
    type("Preset", "github");
    enter("o/a");
    apply();
    expect(screen.getByLabelText("Display name")).toHaveValue("GitHub (o/a)");
    expect(screen.getByLabelText("Description")).toHaveValue("Limited to o/a");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(vi.mocked(onSave).mock.calls[0][1]).toMatchObject({
      display_name: "GitHub (o/a)",
      description: "Limited to o/a",
    });
  });

  it("lists every repository in the labels and keeps what the user already typed", async () => {
    await renderGithub();
    openMoreOptions();
    type("Display name", "Work GitHub");
    enter("o/a\no/b\np/*");
    apply();
    expect(screen.getByLabelText("Display name")).toHaveValue("Work GitHub");
    expect(screen.getByLabelText("Description")).toHaveValue(
      "Limited to o/a, o/b, p/*",
    );
  });

  it("leaves the labels alone when the repositories are not valid", async () => {
    await renderGithub();
    enter("not a repo");
    apply();
    openMoreOptions();
    expect(screen.getByLabelText("Display name")).toHaveValue("");
    expect(screen.getByLabelText("Description")).toHaveValue("");
  });
});

describe("the help beside refusing other paths", () => {
  const REFUSAL =
    "Requests to these hosts whose path matches none of your rules are refused (path_not_allowed) instead of being sent without a key.";
  const BOUNDARY =
    "The real boundary is the key itself, so scope it as narrowly as the website allows.";
  const LIMIT_FOR = {
    user_agent: "this agent only",
    user: "all your agents that share your personal workspace",
    shared: "everyone using this agent",
    unscoped: "every agent without a scope",
  } as const;
  const limit = (scope: keyof typeof LIMIT_FOR) =>
    `This also limits services from your administrator on these hosts, for ${LIMIT_FOR[scope]}.`;
  const helpFor = (scope: keyof typeof LIMIT_FOR) =>
    `${REFUSAL} ${limit(scope)} ${BOUNDARY}`;
  const HELP = helpFor("user_agent");
  const GITHUB_HELP =
    "GraphQL (/graphql) is refused unless listed, and a listed GraphQL rule reaches every repository the key can reach. Use a fine-grained token or a GitHub App installed on selected repositories.";
  const restrictHelp = () =>
    screen
      .getByLabelText("Refuse other paths on these hosts")
      .closest("div") as HTMLElement;

  it("is generic for a service on any other host", async () => {
    await renderEditor({
      scope: "user_agent",
      name: "web",
      service: { rules: [{ host: "httpbin.org", auth: { type: "bearer" } }] },
    });
    openMoreOptions();
    expect(restrictHelp()).toHaveTextContent(HELP);
    expect(restrictHelp()).not.toHaveTextContent("GraphQL");
    expect(restrictHelp()).not.toHaveTextContent("fine-grained");
  });

  it("adds the GraphQL sentence on a GitHub host", async () => {
    await renderEditor({
      scope: "user_agent",
      name: "code",
      service: { preset: "github" },
    });
    openMoreOptions();
    expect(restrictHelp()).toHaveTextContent(`${HELP} ${GITHUB_HELP}`);
    cleanup();
    await renderEditor({
      scope: "user_agent",
      name: "code",
      service: {
        rules: [{ host: "api.github.com", auth: { type: "bearer" } }],
      },
    });
    openMoreOptions();
    expect(restrictHelp()).toHaveTextContent(GITHUB_HELP);
  });

  it("follows the host as it is typed", async () => {
    await renderEditor();
    openMoreOptions();
    expect(restrictHelp()).not.toHaveTextContent("GraphQL");
    type("Website", "api.github.com");
    expect(restrictHelp()).toHaveTextContent("GraphQL");
  });

  it.each(Object.keys(LIMIT_FOR) as (keyof typeof LIMIT_FOR)[])(
    "says whom the limit on the administrator's services reaches on a %s scope",
    async (scope) => {
      await renderEditor({ scope });
      openMoreOptions();
      expect(restrictHelp()).toHaveTextContent(helpFor(scope));
      // The old note about a broader rule of another service is gone.
      expect(screen.queryByRole("note")).toBeNull();
      expect(screen.queryByText(/A broader rule of/)).toBeNull();
    },
  );

  it("names only the agent for a user_agent scope, never the whole workspace", async () => {
    await renderEditor({ scope: "user_agent" });
    openMoreOptions();
    expect(restrictHelp()).toHaveTextContent("for this agent only.");
    expect(restrictHelp()).not.toHaveTextContent("personal workspace");
    expect(restrictHelp()).not.toHaveTextContent("everyone");
  });

  it("makes no claim about whom the limit reaches when the scope is not known", async () => {
    await renderEditor();
    openMoreOptions();
    expect(restrictHelp()).toHaveTextContent(`${REFUSAL} ${BOUNDARY}`);
    expect(restrictHelp()).not.toHaveTextContent("from your administrator");
  });

  it("leaves that sentence out on the dashboard, where the administrator edits config.yaml", async () => {
    await renderEditor({ context: "operator" });
    openMoreOptions();
    expect(restrictHelp()).toHaveTextContent(
      "Requests to these hosts whose path matches none of your rules are refused (path_not_allowed) instead of being sent without a key. The real boundary is the key itself, so scope it as narrowly as the website allows.",
    );
    expect(restrictHelp()).not.toHaveTextContent("from your administrator");
  });
});

describe("loading the presets", () => {
  it("offers what the server returns, by display name", async () => {
    await renderEditor({
      loadPresets: catalog({
        presets: [
          {
            id: "internal",
            display_name: "Internal API",
            description: "",
            oauth_provider: null,
            rules: [
              {
                host: "api.internal.example.com",
                port: 8443,
                path_prefix: "/v1/",
              },
            ],
            placeholder_env: {},
          },
        ],
      }),
    });
    expect(
      within(screen.getByLabelText("Preset"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Custom", "Internal API"]);
    type("Preset", "internal");
    expect(
      screen.getByText(
        "The Internal API preset covers api.internal.example.com:8443/v1/.",
      ),
    ).toBeInTheDocument();
  });

  it("waits for the presets before the form can be saved", async () => {
    let resolve: (catalog: EgressPresetCatalog) => void = () => {};
    const loadPresets = vi.fn(
      () =>
        new Promise<EgressPresetCatalog>((done) => {
          resolve = done;
        }),
    );
    render(<EgressServiceEditor {...baseProps()} loadPresets={loadPresets} />);
    expect(screen.getByLabelText("Preset")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save service" })).toBeDisabled();
    resolve({ presets, oauth_providers: providers, validation });
    await presetsReady();
    expect(screen.getByRole("button", { name: "Save service" })).toBeEnabled();
    expect(loadPresets).toHaveBeenCalledTimes(1);
  });

  it("falls back to a custom service when they cannot be loaded", async () => {
    const onSave = vi.fn(async () => undefined);
    await renderEditor({
      loadPresets: failingPresets(),
      onSave,
    });
    expect(
      screen.getByText(/The preset list could not be loaded/),
    ).toBeInTheDocument();
    type("Name", "own");
    type("Website", "a.example.com");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("own", {
      display_name: "a.example.com",
      rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
    });
  });

  it("keeps a preset the server no longer lists", async () => {
    await renderEditor({
      name: "old",
      service: { preset: "retired" },
      loadPresets: catalog({ presets: [] }),
    });
    expect(screen.getByLabelText("Preset")).toHaveValue("retired");
  });
});

describe("the rule cap from the server", () => {
  const twoRules = {
    rules: [bearerRule, { ...bearerRule, host: "b.example.com" }],
  };
  const addRule = () => screen.getByRole("button", { name: "Add rule" });
  const editWith = (changes: Partial<EgressPresetCatalog>) =>
    renderEditor({
      name: "svc",
      service: twoRules,
      loadPresets: catalog(changes),
    });

  it("stops adding rules at the most the server takes", async () => {
    await editWith({
      validation: { ...validation, max_rules_per_service: 2 },
    });
    expect(addRule()).toBeDisabled();
  });

  it("keeps adding rules below it, up to it", async () => {
    await editWith({
      validation: { ...validation, max_rules_per_service: 3 },
    });
    expect(addRule()).toBeEnabled();
    fireEvent.click(addRule());
    expect(addRule()).toBeDisabled();
  });

  it("leaves the count to the server when the presets could not be loaded", async () => {
    await renderEditor({
      name: "svc",
      service: twoRules,
      loadPresets: failingPresets(),
    });
    expect(addRule()).toBeEnabled();
  });
});

describe("saving with a wider effect", () => {
  const saveWarning = "This also saves your other unsaved settings changes.";
  const filled = async (props: Partial<EgressServiceEditorProps> = {}) => {
    const rendered = await renderEditor({ saveWarning, ...props });
    type("Name", "svc");
    type("Preset", "openai");
    return rendered;
  };

  it("asks once before saving, then saves", async () => {
    const { onSave } = await filled();
    save();
    expect(screen.getByRole("alert")).toHaveTextContent(saveWarning);
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Save all changes" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("svc", { preset: "openai" });
  });

  it("goes back without saving", async () => {
    const { onSave } = await filled();
    save();
    fireEvent.click(screen.getByRole("button", { name: "Go back" }));
    expect(screen.queryByText(saveWarning)).toBeNull();
    expect(screen.getByRole("button", { name: "Save service" })).toBeEnabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("asks again after the form changes", async () => {
    const { onSave } = await filled();
    save();
    openMoreOptions();
    type("Description", "changed");
    expect(screen.queryByText(saveWarning)).toBeNull();
    save();
    expect(screen.getByText(saveWarning)).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not ask for an invalid form", async () => {
    await renderEditor({ saveWarning });
    save();
    expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(
      /Name is required/,
    );
    expect(screen.queryByText(saveWarning)).toBeNull();
  });

  it("saves straight away without a warning", async () => {
    const { onSave } = await filled({ saveWarning: null });
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  it("repeats the warning when asking to delete", async () => {
    await renderGithub({ onDelete: vi.fn(), saveWarning });
    fireEvent.click(screen.getByRole("button", { name: "Delete service" }));
    expect(
      screen.getByText(`Delete github? ${saveWarning}`),
    ).toBeInTheDocument();
  });
});

describe("the question before deleting from inside the editor", () => {
  const ask = async (props: Partial<EgressServiceEditorProps>) => {
    await renderEditor({
      name: "mine",
      service: { preset: "github" },
      onDelete: vi.fn(),
      ...props,
    });
    fireEvent.click(screen.getByRole("button", { name: "Delete service" }));
    return screen.getByRole("button", { name: "Confirm delete" }).parentElement;
  };

  it("says that the one agent loses access on a user_agent scope", async () => {
    expect(
      await ask({ scope: "user_agent", agentName: "Coder", scopeAgents: [] }),
    ).toHaveTextContent(
      "Delete mine? This deletes the service and its saved key. Your Coder loses access through it.",
    );
  });

  it("names every agent that shares the workspace on a user scope", async () => {
    expect(
      await ask({
        scope: "user",
        agentName: "Coder",
        scopeAgents: ["Coder", "Writer"],
      }),
    ).toHaveTextContent(
      "Delete mine? This deletes the service and its saved key. All your agents that share your personal workspace lose access through it: Coder, Writer.",
    );
  });

  it.each([
    ["shared", "Everyone using Team Bot loses access through it."],
    ["unscoped", "Every agent without a scope loses access through it."],
  ] as const)(
    "says who loses access on a %s scope",
    async (scope, consequence) => {
      expect(
        await ask({ scope, agentName: "Team Bot", scopeAgents: [] }),
      ).toHaveTextContent(
        `Delete mine? This deletes the service and its shared key. ${consequence}`,
      );
    },
  );

  it("keeps the other-unsaved-changes warning after the scope sentence", async () => {
    expect(
      await ask({
        scope: "user_agent",
        agentName: "Coder",
        saveWarning: "This also saves your other unsaved settings changes.",
      }),
    ).toHaveTextContent(
      "Delete mine? This deletes the service and its saved key. Your Coder loses access through it. This also saves your other unsaved settings changes.",
    );
  });

  it("asks only the plain question without a scope, as on the dashboard", async () => {
    const asked = await ask({ context: "operator" });
    expect(asked).toHaveTextContent(/^Delete mine\?Confirm deleteKeep$/);
    cleanup();
    // Even a scope handed to the dashboard editor changes nothing there.
    const withScope = await ask({
      context: "operator",
      scope: "shared",
      agentName: "Team Bot",
    });
    expect(withScope).not.toHaveTextContent("loses access");
  });
});

describe("reporting unsaved changes", () => {
  it("tells the parent when the form differs from what it opened with", async () => {
    const onDirtyChange = vi.fn();
    await renderGithub({ onDirtyChange });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);

    openMoreOptions();
    type("Description", "x");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    type("Description", "");
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });
});

describe("a service with values emptied on purpose", () => {
  it("shows what the empty list means and saves it back unchanged", async () => {
    const { onSave } = await renderEditor({
      name: "github",
      service: { preset: "github", placeholder_env: {}, description: "" },
    });
    expect(screen.queryByLabelText("Placeholder 1 name")).toBeNull();
    openMoreOptions();
    expect(
      screen.getByText(
        /This service sets none, which drops the placeholders of the GitHub preset/,
      ),
    ).toBeInTheDocument();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", {
      preset: "github",
      description: "",
      placeholder_env: {},
    });
  });

  it("offers the preset's placeholders when the service has not set any", async () => {
    await renderGithub();
    openMoreOptions();
    expect(
      screen.getByText(
        /Leave the list empty to use the placeholders of the GitHub preset/,
      ),
    ).toBeInTheDocument();
  });
});

describe("choosing another preset", () => {
  it("forgets what the loaded service emptied on purpose", async () => {
    const { onSave } = await renderEditor({
      name: "mine",
      service: {
        preset: "openai",
        display_name: null,
        description: "",
        placeholder_env: {},
      },
    });
    type("Preset", "github");
    openMoreOptions();
    // The GitHub preset has placeholders of its own, so the list is not "none".
    expect(
      screen.getByText(
        /Leave the list empty to use the placeholders of the GitHub preset/,
      ),
    ).toBeInTheDocument();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("mine", { preset: "github" });
  });
});

describe("the GitHub helper on a custom service", () => {
  const rulesFor = (repository: string) => [
    {
      host: "api.github.com",
      path_prefix: `/repos/${repository}`,
      auth: { type: "bearer" },
    },
    {
      host: "github.com",
      path_prefix: `/${repository}`,
      auth: { type: "basic", username: "x-access-token" },
    },
    {
      host: "github.com",
      path_prefix: `/${repository}.git`,
      auth: { type: "basic", username: "x-access-token" },
    },
  ];

  it("adds the GH_TOKEN and GITHUB_TOKEN placeholders and shows them", async () => {
    const { onSave } = await renderEditor();
    type("Name", "code");
    type("Website", "api.github.com");
    expect(screen.queryByLabelText("Placeholder 1 name")).toBeNull();
    enter("o/r");
    apply();

    // More options opens so the user sees what was added.
    expect(screen.getByLabelText("Placeholder 1 name")).toHaveValue("GH_TOKEN");
    expect(screen.getByLabelText("Placeholder 1 value")).toHaveValue(
      "mindroom-brokered",
    );
    expect(screen.getByLabelText("Placeholder 2 name")).toHaveValue(
      "GITHUB_TOKEN",
    );
    expect(screen.getByLabelText("Placeholder 2 value")).toHaveValue(
      "mindroom-brokered",
    );
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("code", {
      display_name: "GitHub (o/r)",
      description: "Limited to o/r",
      rules: rulesFor("o/r"),
      restrict_to_rules: true,
      placeholder_env: {
        GH_TOKEN: "mindroom-brokered",
        GITHUB_TOKEN: "mindroom-brokered",
      },
    });
  });

  it("leaves the placeholders a service already has alone", async () => {
    const { onSave } = await renderEditor({
      name: "code",
      service: {
        rules: [{ host: "api.github.com", auth: { type: "bearer" } }],
        placeholder_env: { MY_GITHUB_TOKEN: "mindroom-brokered" },
      },
    });
    enter("o/r");
    apply();
    expect(screen.getByLabelText("Placeholder 1 name")).toHaveValue(
      "MY_GITHUB_TOKEN",
    );
    expect(screen.queryByLabelText("Placeholder 2 name")).toBeNull();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("code", {
      display_name: "GitHub (o/r)",
      description: "Limited to o/r",
      rules: rulesFor("o/r"),
      restrict_to_rules: true,
      placeholder_env: { MY_GITHUB_TOKEN: "mindroom-brokered" },
    });
  });

  it("adds them only once when the repositories are applied again", async () => {
    await renderEditor();
    type("Website", "api.github.com");
    enter("o/r");
    apply();
    enter("o/other");
    apply();
    expect(screen.getByLabelText("Placeholder 2 name")).toHaveValue(
      "GITHUB_TOKEN",
    );
    expect(screen.queryByLabelText("Placeholder 3 name")).toBeNull();
  });

  it("renames the labels it filled when applied again, but never ones the user typed", async () => {
    await renderEditor();
    type("Website", "api.github.com");
    enter("o/r");
    apply();
    enter("o/other");
    apply();
    expect(screen.getByLabelText("Display name")).toHaveValue(
      "GitHub (o/other)",
    );
    expect(screen.getByLabelText("Description")).toHaveValue(
      "Limited to o/other",
    );
    type("Description", "My own words");
    enter("o/third");
    apply();
    expect(screen.getByLabelText("Display name")).toHaveValue(
      "GitHub (o/third)",
    );
    expect(screen.getByLabelText("Description")).toHaveValue("My own words");
  });

  it("adds none to a preset service, which brings its own", async () => {
    const { onSave } = await renderGithub();
    enter("o/r");
    apply();
    expect(screen.queryByLabelText("Placeholder 1 name")).toBeNull();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(vi.mocked(onSave).mock.calls[0][1]).not.toHaveProperty(
      "placeholder_env",
    );
  });
});

describe("a form after a failed save", () => {
  it("keeps the input, clears the error with the next try, and saves the later edits", async () => {
    const onSave = vi
      .fn<(name: string, service: object) => Promise<void>>()
      .mockRejectedValueOnce(new Error("The server said no"))
      .mockResolvedValue(undefined);
    await renderEditor({ onSave });
    type("Name", "svc");
    type("Website", "a.example.com");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server said no",
    );
    expect(screen.getByLabelText("Name")).toHaveValue("svc");
    expect(screen.getByLabelText("Website")).toHaveValue("a.example.com");
    expect(screen.getByRole("button", { name: "Save service" })).toBeEnabled();

    type("Website", "b.example.com");
    openMoreOptions();
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    type("Rule 2 host", "c.example.com");
    save();

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(onSave).toHaveBeenLastCalledWith("svc", {
      display_name: "b.example.com",
      rules: [
        { host: "b.example.com", auth: { type: "bearer" } },
        { host: "c.example.com", auth: { type: "bearer" } },
      ],
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a validation problem found after the server error instead of saving", async () => {
    const onSave = vi.fn(async () => {
      throw new Error("The server said no");
    });
    await renderEditor({ onSave });
    type("Name", "svc");
    type("Website", "a.example.com");
    save();
    await screen.findByText("The server said no");

    type("Website", "a.example.com:8443");
    expect(screen.getByLabelText("Website")).toHaveAccessibleDescription(
      /Host must not contain a port/,
    );
    // The server's answer gives way to what the form itself can see.
    expect(screen.queryByText("The server said no")).toBeNull();
    save();
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});

describe("the account login field", () => {
  it("is a select of the providers the server lists, with none as the first choice", async () => {
    await renderEditor();
    openMoreOptions();
    const login = screen.getByLabelText("Account login (optional)");
    expect(login.tagName).toBe("SELECT");
    expect(login).toHaveValue("");
    expect(
      within(login)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["No account login", ...providers.map((p) => p.display_name)]);
    fireEvent.change(login, { target: { value: "google_gmail" } });
    expect(login).toHaveValue("google_gmail");
  });

  it("offers no provider the server does not list, but keeps one the service has", async () => {
    await renderEditor({
      name: "svc",
      service: {
        rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
        oauth_provider: "retired_login",
      },
      loadPresets: catalog({ oauth_providers: [] }),
    });
    const login = screen.getByLabelText("Account login (optional)");
    expect(login).toHaveValue("retired_login");
    expect(
      within(login)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["No account login", "retired_login"]);
  });

  it("still refuses a stored value that is not a provider id", async () => {
    const { onSave } = await renderEditor({
      name: "svc",
      service: {
        rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
        oauth_provider: "Git Hub",
      },
    });
    save();
    expect(
      screen.getByLabelText("Account login (optional)"),
    ).toHaveAccessibleDescription(/Account login must be a provider id/);
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("the basic form", () => {
  const before = (first: HTMLElement, second: HTMLElement) =>
    Boolean(
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
    );
  const MORE_FIELDS = [
    "Display name",
    "Description",
    "Path prefix",
    "Port",
    "Refuse other paths on these hosts",
    "Account login (optional)",
  ];

  it("shows the basic fields first and the others under More options", async () => {
    await renderEditor({ saveKey: vi.fn(async () => undefined) });
    // The website comes before the name, which follows from it.
    const basic = ["Preset", "Website", "Send the key as", "Name", "API key"];
    const fields = basic.map((label) => screen.getByLabelText(label));
    fields.slice(1).forEach((field, index) => {
      expect(before(fields[index], field)).toBe(true);
    });
    const more = screen.getByRole("button", { name: "More options" });
    for (const field of fields) expect(before(field, more)).toBe(true);
    expect(more).toHaveAttribute("aria-expanded", "false");
    for (const label of MORE_FIELDS)
      expect(screen.queryByLabelText(label)).toBeNull();
    expect(screen.queryByText("Placeholder variables")).toBeNull();

    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    for (const label of MORE_FIELDS)
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    expect(screen.getByText("Placeholder variables")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Add rule" }),
    ).toBeInTheDocument();
    // Everything under More options comes after the toggle.
    for (const label of MORE_FIELDS)
      expect(before(more, screen.getByLabelText(label))).toBe(true);

    fireEvent.click(more);
    expect(screen.queryByLabelText("Display name")).toBeNull();
  });

  it("points More options at its section only while that section is there", async () => {
    await renderEditor();
    const more = screen.getByRole("button", { name: "More options" });
    expect(more).not.toHaveAttribute("aria-controls");
    fireEvent.click(more);
    const section = document.getElementById(
      more.getAttribute("aria-controls") ?? "",
    );
    expect(section).not.toBeNull();
    expect(section).toContainElement(screen.getByLabelText("Display name"));
    fireEvent.click(more);
    expect(more).not.toHaveAttribute("aria-controls");
  });

  it("asks for a header name only when the key goes in a header", async () => {
    await renderEditor();
    expect(screen.queryByLabelText("Header name")).toBeNull();
    type("Send the key as", "header");
    expect(screen.getByLabelText("Header name")).toBeInTheDocument();
  });

  it("explains the header name with an example and does not prefill one", async () => {
    await renderEditor();
    type("Send the key as", "header");
    const field = screen.getByLabelText("Header name");
    expect(field).toHaveAccessibleDescription("For example X-Api-Key");
    expect(field).not.toHaveAttribute("placeholder");
    expect(field).toHaveValue("");
    type("Send the key as", "query");
    expect(screen.getByLabelText("Parameter name")).not.toHaveAttribute(
      "placeholder",
    );
  });

  it("keeps the first rule's host, path, port, and way of sending in step", async () => {
    await renderEditor({
      name: "svc",
      service: {
        rules: [
          {
            host: "api.example.com",
            port: 8443,
            path_prefix: "/v1/",
            auth: { type: "basic", username: "bot" },
          },
        ],
      },
    });
    expect(screen.getByLabelText("Website")).toHaveValue("api.example.com");
    expect(screen.getByLabelText("Send the key as")).toHaveValue("basic");
    expect(screen.getByLabelText("Username")).toHaveValue("bot");
    // It has path and port, so More options is already open.
    expect(screen.getByLabelText("Path prefix")).toHaveValue("/v1/");
    expect(screen.getByLabelText("Port")).toHaveValue("8443");
  });
});

describe("the name of a new service", () => {
  it("follows the preset id and then the host's first label until the user types one", async () => {
    await renderEditor();
    type("Website", "httpbin.org");
    expect(screen.getByLabelText("Name")).toHaveValue("httpbin");
    type("Website", "api.example.com");
    expect(screen.getByLabelText("Name")).toHaveValue("api");
    type("Preset", "github");
    expect(screen.getByLabelText("Name")).toHaveValue("github");
    type("Preset", "");
    expect(screen.getByLabelText("Name")).toHaveValue("api");
    type("Preset", "google_drive");
    expect(screen.getByLabelText("Name")).toHaveValue("google_drive");

    type("Name", "mine");
    type("Preset", "openai");
    expect(screen.getByLabelText("Name")).toHaveValue("mine");
    type("Preset", "");
    type("Website", "other.example.com");
    expect(screen.getByLabelText("Name")).toHaveValue("mine");
  });

  it("follows again after the user empties the name", async () => {
    await renderEditor();
    type("Name", "mine");
    type("Name", "");
    type("Website", "httpbin.org");
    expect(screen.getByLabelText("Name")).toHaveValue("httpbin");
  });

  it("saves the suggested name", async () => {
    const { onSave } = await renderEditor();
    type("Website", "httpbin.org");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("httpbin", {
      display_name: "httpbin.org",
      rules: [{ host: "httpbin.org", auth: { type: "bearer" } }],
    });
  });

  it("never renames a service that is being edited", async () => {
    await renderEditor({
      name: "mine",
      service: { rules: [{ host: "a.example.com", auth: { type: "bearer" } }] },
    });
    type("Website", "b.example.com");
    expect(screen.getByLabelText("Name")).toHaveValue("mine");
  });

  it("says Name is required, beside the name, and focuses the website first", async () => {
    const { onSave } = await renderEditor();
    save();
    expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(
      /Name is required/,
    );
    // The website is the first field on the page, and the name follows from it.
    expect(screen.getByLabelText("Website")).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("errors beside their fields", () => {
  it("shows each problem next to its field and focuses the first invalid one", async () => {
    const { onSave } = await renderEditor();
    save();
    const name = screen.getByLabelText("Name");
    const website = screen.getByLabelText("Website");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveAccessibleDescription(/Name is required/);
    expect(website).toHaveAttribute("aria-invalid", "true");
    expect(website).toHaveAccessibleDescription(/Host is required/);
    // The website comes first on the page.
    expect(website).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
    // Fields without a problem are not marked.
    expect(screen.getByLabelText("Preset")).not.toHaveAttribute("aria-invalid");

    // The messages follow the form once the user has tried to save, and the
    // name follows the website until the person types one.
    type("Website", "a.example.com");
    expect(website).not.toHaveAttribute("aria-invalid");
    expect(name).toHaveValue("a");
    expect(name).not.toHaveAttribute("aria-invalid");
  });

  it("opens More options for a problem in it, and focuses that field", async () => {
    const { onSave } = await renderEditor();
    type("Website", "a.example.com");
    openMoreOptions();
    type("Path prefix", "v1");
    openMoreOptions();
    expect(screen.queryByLabelText("Path prefix")).toBeNull();
    save();
    const path = screen.getByLabelText("Path prefix");
    expect(path).toHaveAccessibleDescription(/Path prefix must start with \//);
    expect(path).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows a problem of an extra rule inside that rule", async () => {
    await renderEditor();
    type("Website", "a.example.com");
    openMoreOptions();
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    save();
    const host = screen.getByLabelText("Rule 2 host");
    expect(host).toHaveAccessibleDescription(/Host is required/);
    expect(host).toHaveFocus();
  });

  it("shows the field of a header, a template, or a port", async () => {
    await renderEditor();
    type("Name", "svc");
    type("Website", "a.example.com");
    type("Send the key as", "template");
    openMoreOptions();
    type("Port", "99999");
    save();
    expect(screen.getByLabelText("Header name")).toHaveAccessibleDescription(
      /Header auth needs a header name/,
    );
    expect(screen.getByLabelText("Value template")).toHaveAccessibleDescription(
      /Template must contain \{secret\} exactly once/,
    );
    expect(screen.getByLabelText("Port")).toHaveAccessibleDescription(
      /Port must be a number from 1 to 65535/,
    );
    // The header name comes first on the page.
    expect(screen.getByLabelText("Header name")).toHaveFocus();
  });

  it("keeps showing the server's own error in an alert", async () => {
    const onSave = vi.fn(async () => {
      throw new Error("Could not save the service.");
    });
    await renderEditor({ onSave });
    type("Website", "a.example.com");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save the service.",
    );
    expect(screen.getByLabelText("Name")).not.toHaveAttribute("aria-invalid");
  });

  it("states a size problem for the whole form in an alert", async () => {
    await renderEditor();
    type("Website", "a.example.com");
    openMoreOptions();
    type("Description", "x".repeat(17 * 1024));
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A service can take at most 16 KiB",
    );
  });
});

describe("saving the API key with the service", () => {
  const fill = () => {
    type("Name", "httpbin");
    type("Website", "httpbin.org");
  };

  it("offers an optional password field for the key", async () => {
    await renderEditor({ saveKey: vi.fn(async () => undefined) });
    const field = screen.getByLabelText("API key");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAccessibleDescription("Paste the key now or later");
    expect(field).toHaveAttribute("autocomplete", "new-password");
  });

  it("leaves the key field out when the page cannot save keys", async () => {
    await renderEditor();
    expect(screen.queryByLabelText("API key")).toBeNull();
  });

  it("writes the service first and the key second, then reports it saved", async () => {
    const calls: string[] = [];
    const onSave = vi.fn(async () => {
      calls.push("service");
    });
    const saveKey = vi.fn(async () => {
      calls.push("key");
    });
    const onClose = vi.fn(() => {
      calls.push("saved");
    });
    await renderEditor({ onSave, saveKey, onClose });
    fill();
    type("API key", "sk-123");
    save();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(calls).toEqual(["service", "key", "saved"]);
    expect(saveKey).toHaveBeenCalledWith("sk-123", "httpbin");
    expect(onSave).toHaveBeenCalledWith("httpbin", {
      display_name: "httpbin.org",
      rules: [{ host: "httpbin.org", auth: { type: "bearer" } }],
    });
  });

  it("saves only the service when no key was typed", async () => {
    const saveKey = vi.fn(async () => undefined);
    const onClose = vi.fn();
    const { onSave } = await renderEditor({ saveKey, onClose });
    fill();
    type("API key", "   ");
    save();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(saveKey).not.toHaveBeenCalled();
  });

  it("never saves a key for a service whose save failed", async () => {
    const onSave = vi.fn(async () => {
      throw new Error("The server said no");
    });
    const saveKey = vi.fn(async () => undefined);
    const onClose = vi.fn();
    await renderEditor({ onSave, saveKey, onClose });
    fill();
    type("API key", "sk-123");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server said no",
    );
    expect(saveKey).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    // The key stays typed for the next try.
    expect(screen.getByLabelText("API key")).toHaveValue("sk-123");
    expect(screen.getByLabelText("Name")).toBeEnabled();
  });

  it("says when the key failed, keeps the service, and retries only the key", async () => {
    const onSave = vi.fn(async () => undefined);
    const saveKey = vi
      .fn<(key: string, name: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error("The vault is read-only"))
      .mockResolvedValue(undefined);
    const onClose = vi.fn();
    await renderEditor({ onSave, saveKey, onClose });
    fill();
    type("API key", "sk-123");
    save();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "The service was saved, but its key was not: The vault is read-only",
    );
    expect(alert).toHaveTextContent("Paste the key again and save to retry.");
    expect(onClose).not.toHaveBeenCalled();
    // The saved service stays: it keeps its name, and the form stays open.
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(
      screen.getByRole("form", { name: "Add service" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("API key")).toHaveValue("sk-123");

    save();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(saveKey).toHaveBeenCalledTimes(2);
    expect(saveKey).toHaveBeenLastCalledWith("sk-123", "httpbin");
    // Nothing changed in the service, so it is not written again.
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("writes the service again when it changed after the key failed", async () => {
    const onSave = vi.fn(async () => undefined);
    const saveKey = vi
      .fn<(key: string, name: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error("The vault is read-only"))
      .mockResolvedValue(undefined);
    await renderEditor({ onSave, saveKey });
    fill();
    type("API key", "sk-123");
    save();
    await screen.findByRole("alert");
    type("Website", "other.example.com");
    save();
    await waitFor(() => expect(saveKey).toHaveBeenCalledTimes(2));
    expect(onSave).toHaveBeenCalledTimes(2);
    expect(onSave).toHaveBeenLastCalledWith("httpbin", {
      display_name: "other.example.com",
      rules: [{ host: "other.example.com", auth: { type: "bearer" } }],
    });
  });

  it("counts a typed key as unsaved changes", async () => {
    const onDirtyChange = vi.fn();
    await renderGithub({
      saveKey: vi.fn(async () => undefined),
      onDirtyChange,
    });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    type("API key", "sk-1");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
  });

  it("can set a key while editing a service", async () => {
    const saveKey = vi.fn(async () => undefined);
    const onClose = vi.fn();
    await renderGithub({ saveKey, onClose });
    type("API key", "ghp_x");
    save();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(saveKey).toHaveBeenCalledWith("ghp_x", "github");
  });
});

describe("a placeholder an administrator's service also sets", () => {
  const configPlaceholders = { GH_TOKEN: "GitHub" };
  const warning =
    "GH_TOKEN is also set by GitHub from your administrator; when both are set, theirs is used.";

  it("warns with the service's display name, and only for that variable", async () => {
    const { onSave } = await renderEditor({ configPlaceholders });
    type("Name", "svc");
    type("Website", "a.example.com");
    openMoreOptions();
    fireEvent.click(screen.getByRole("button", { name: "Add placeholder" }));
    type("Placeholder 1 name", "MY_API_KEY");
    expect(screen.queryByText(warning)).toBeNull();
    type("Placeholder 1 name", "GH_TOKEN");
    expect(screen.getByText(warning)).toBeInTheDocument();
    expect(
      screen.getByLabelText("Placeholder 1 name"),
    ).toHaveAccessibleDescription(warning);
    // It is a warning: the service still saves.
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("svc", {
      display_name: "a.example.com",
      rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
      placeholder_env: { GH_TOKEN: "mindroom-brokered" },
    });
  });

  it("does not mark the field as invalid", async () => {
    await renderEditor({
      configPlaceholders,
      name: "svc",
      service: { preset: "github", placeholder_env: { GH_TOKEN: "x" } },
    });
    expect(screen.getByText(warning)).toBeInTheDocument();
    expect(screen.getByLabelText("Placeholder 1 name")).not.toHaveAttribute(
      "aria-invalid",
    );
  });

  it("warns for the placeholders the GitHub helper adds", async () => {
    await renderEditor({ configPlaceholders });
    type("Website", "api.github.com");
    enter("o/r");
    apply();
    expect(screen.getByText(warning)).toBeInTheDocument();
  });

  it("says nothing without any", async () => {
    await renderEditor({
      name: "svc",
      service: { preset: "github", placeholder_env: { GH_TOKEN: "x" } },
    });
    expect(screen.queryByText(/from your administrator; when both/)).toBeNull();
  });
});

describe("the note on whose service this is", () => {
  it("names the agent for a user_agent service", async () => {
    await renderEditor({
      scope: "user_agent",
      agentName: "Code Helper",
      scopeAgents: [],
    });
    expect(
      screen.getByText("Only your Code Helper uses this service and its key."),
    ).toBeInTheDocument();
  });

  it("lists the agents that share a user's workspace", async () => {
    await renderEditor({
      scope: "user",
      agentName: "Code Helper",
      scopeAgents: ["Code Helper", "Research"],
    });
    expect(
      screen.getByText(
        "All your agents that share your personal workspace use this service and its key: Code Helper, Research.",
      ),
    ).toBeInTheDocument();
  });

  it("says nothing for a shared agent or the dashboard, and drops the old note", async () => {
    const oldNote =
      "This service applies to all of your personal agents that share this scope.";
    await renderEditor({
      scope: "shared",
      agentName: "Team",
      sharedTarget: true,
    });
    expect(screen.queryByText(/uses this service and its key/)).toBeNull();
    expect(screen.queryByText(/use this service and its key/)).toBeNull();
    expect(screen.queryByText(oldNote)).toBeNull();
    cleanup();
    await renderEditor({ context: "operator" });
    expect(screen.queryByText(/service and its key/)).toBeNull();
    expect(screen.queryByText(oldNote)).toBeNull();
    cleanup();
    await renderEditor();
    expect(screen.queryByText(oldNote)).toBeNull();
  });
});

describe("a free name for a new service", () => {
  const nameField = () => screen.getByLabelText("Name");

  it("skips the names the page already lists", async () => {
    await renderEditor({ takenNames: ["github"] });
    type("Preset", "github");
    expect(nameField()).toHaveValue("github-2");
    cleanup();
    await renderEditor({ takenNames: ["github", "github-2"] });
    type("Preset", "github");
    expect(nameField()).toHaveValue("github-3");
  });

  it("does the same for a name taken from the host", async () => {
    await renderEditor({ takenNames: ["httpbin"] });
    type("Website", "httpbin.org");
    expect(nameField()).toHaveValue("httpbin-2");
  });

  it("names the clash when all three are taken", async () => {
    const { onSave } = await renderEditor({
      takenNames: ["github", "github-2", "github-3"],
    });
    type("Preset", "github");
    expect(nameField()).toHaveValue("github");
    save();
    expect(nameField()).toHaveAccessibleDescription(
      /A service named github already exists/,
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("saves the free name it suggested", async () => {
    const { onSave } = await renderEditor({ takenNames: ["github"] });
    type("Preset", "github");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github-2", { preset: "github" });
  });

  it("never changes a name the user typed", async () => {
    await renderEditor({ takenNames: ["github"] });
    type("Name", "github");
    type("Preset", "github");
    expect(nameField()).toHaveValue("github");
  });
});

describe("the name after the GitHub helper", () => {
  it("keeps the name when the helper replaces the rules", async () => {
    await renderEditor();
    type("Website", "github.com");
    expect(screen.getByLabelText("Name")).toHaveValue("github");
    // The helper's first rule is on api.github.com, whose first label is "api".
    enter("o/r");
    apply();
    expect(screen.getByLabelText("Website")).toHaveValue("api.github.com");
    expect(screen.getByLabelText("Name")).toHaveValue("github");
  });

  it("keeps it after the user edits the website too, as a name the helper fixed", async () => {
    await renderEditor();
    type("Website", "github.com");
    enter("o/r");
    apply();
    type("Website", "uploads.github.com");
    expect(screen.getByLabelText("Name")).toHaveValue("github");
  });

  it("keeps the suggested name of the preset", async () => {
    await renderEditor({ takenNames: ["github"] });
    type("Preset", "github");
    enter("o/r");
    apply();
    expect(screen.getByLabelText("Name")).toHaveValue("github-2");
  });
});

describe("a preset's placeholders an administrator's service also sets", () => {
  const configPlaceholders = { GH_TOKEN: "GitHub" };
  const warning =
    "GH_TOKEN is also set by GitHub from your administrator; when both are set, theirs is used.";

  it("warns in the basic form, beside the preset, when the preset brings them", async () => {
    await renderEditor({
      name: "mine",
      service: { preset: "github" },
      configPlaceholders,
    });
    // More options is still collapsed, and the warning is already there.
    expect(screen.queryByLabelText("Display name")).toBeNull();
    expect(screen.getByText(warning)).toBeVisible();
    // GITHUB_TOKEN is not set by the administrator's service.
    expect(screen.queryByText(/GITHUB_TOKEN is also set/)).toBeNull();
  });

  it("shows it once, not again under the placeholder variables", async () => {
    await renderEditor({
      name: "mine",
      service: { preset: "github" },
      configPlaceholders,
    });
    openMoreOptions();
    expect(screen.getByLabelText("Display name")).toBeVisible();
    expect(screen.getAllByText(warning)).toHaveLength(1);
  });

  it("warns on choosing the preset without opening More options", async () => {
    await renderEditor({ configPlaceholders });
    expect(screen.queryByText(warning)).toBeNull();
    type("Preset", "github");
    expect(screen.queryByLabelText("Display name")).toBeNull();
    expect(screen.getByText(warning)).toBeVisible();
    // It follows the chosen preset.
    type("Preset", "openai");
    expect(screen.queryByText(warning)).toBeNull();
  });

  it("stops once the service lists placeholders of its own", async () => {
    await renderEditor({
      name: "mine",
      service: { preset: "github", placeholder_env: { MY_API_KEY: "x" } },
      configPlaceholders,
    });
    expect(screen.queryByText(warning)).toBeNull();
  });

  it("stays quiet when the service drops the preset's placeholders on purpose", async () => {
    await renderEditor({
      name: "mine",
      service: { preset: "github", placeholder_env: {} },
      configPlaceholders,
    });
    expect(screen.queryByText(warning)).toBeNull();
  });
});

describe("which section a problem opens", () => {
  const toggle = () => screen.getByRole("button", { name: "More options" });

  it.each<[string, string, () => void, boolean]>([
    ["the host", "Website", () => type("Website", ""), true],
    ["the username", "Username", () => type("Send the key as", "basic"), true],
    [
      "the header name",
      "Header name",
      () => type("Send the key as", "header"),
      true,
    ],
    [
      "the template",
      "Value template",
      () => {
        type("Send the key as", "template");
        type("Header name", "Authorization");
      },
      true,
    ],
    [
      "the path prefix",
      "Path prefix",
      () => {
        openMoreOptions();
        type("Path prefix", "v1");
        openMoreOptions();
      },
      false,
    ],
    [
      "the port",
      "Port",
      () => {
        openMoreOptions();
        type("Port", "0");
        openMoreOptions();
      },
      false,
    ],
  ])("%s", async (_name, label, arrange, basic) => {
    const { onSave } = await renderEditor();
    type("Name", "svc");
    type("Website", "a.example.com");
    arrange();
    save();
    // A problem in a basic field leaves More options as it was; any other opens it.
    expect(toggle()).toHaveAttribute("aria-expanded", String(!basic));
    expect(screen.getByLabelText(label)).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText(label)).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("focuses the message for a problem of the rule list as a whole", async () => {
    const rules = Array.from({ length: 51 }, (_, index) => ({
      host: `h${index}.example.com`,
      auth: { type: "bearer" as const },
    }));
    const onSave = vi.fn(async () => undefined);
    // Fifty-one rules make a large page, where label and role queries are slow, so query it directly.
    const { container } = render(
      <EgressServiceEditor
        {...baseProps()}
        name="big"
        service={{ rules }}
        onSave={onSave}
      />,
    );
    const submit = container.querySelector<HTMLButtonElement>(
      'button[type="submit"]',
    );
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit as HTMLButtonElement);
    const message = screen.getByText("A service can have at most 50 rules");
    expect(toggle()).toHaveAttribute("aria-expanded", "true");
    expect(message).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("what a collapsed or failed form keeps and shows", () => {
  it("keeps a value typed under More options after the section is collapsed", async () => {
    const { onSave } = await renderEditor();
    type("Name", "svc");
    type("Website", "a.example.com");
    openMoreOptions();
    type("Display name", "Typed here");
    type("Path prefix", "/v1/");
    openMoreOptions();
    expect(screen.queryByLabelText("Display name")).toBeNull();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("svc", {
      display_name: "Typed here",
      rules: [
        {
          host: "a.example.com",
          path_prefix: "/v1/",
          auth: { type: "bearer" },
        },
      ],
    });
    // The save does not reopen the section.
    expect(screen.queryByLabelText("Display name")).toBeNull();
  });

  it("shows the typed value again when the section is reopened", async () => {
    await renderEditor();
    openMoreOptions();
    type("Display name", "Typed here");
    openMoreOptions();
    openMoreOptions();
    expect(screen.getByLabelText("Display name")).toHaveValue("Typed here");
  });

  it("never prints the typed key after the key failed", async () => {
    const saveKey = vi.fn(async () => {
      throw new Error("The vault is read-only");
    });
    await renderEditor({ saveKey });
    type("Name", "svc");
    type("Website", "a.example.com");
    type("API key", "sk-very-secret-123");
    save();
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("sk-very-secret-123");
    expect(document.body.textContent).not.toContain("sk-very-secret-123");
    // It stays in the password field for the retry, and nowhere as text.
    expect(screen.getByLabelText("API key")).toHaveValue("sk-very-secret-123");
    expect(screen.getByLabelText("API key")).toHaveAttribute(
      "type",
      "password",
    );
  });
});

describe("asking before discarding an open editor", () => {
  it("offers to discard or to keep editing", () => {
    const onDiscard = vi.fn();
    const onKeep = vi.fn();
    render(<DiscardEditsNotice onDiscard={onDiscard} onKeep={onKeep} />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "You have unsaved changes in the open editor.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(onKeep).toHaveBeenCalled();
    expect(onDiscard).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(onDiscard).toHaveBeenCalled();
  });
});

describe("repositories typed in the helper and saved without pressing Use these repositories", () => {
  const octocatRules = (graphql = false) =>
    githubRepositoryRules("octocat/Hello-World", { uploads: false, graphql })
      .rules;

  it("applies them on Save, writing the same limited service as pressing the button", async () => {
    const { onSave } = await renderGithub();
    enter("octocat/Hello-World");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", {
      preset: "github",
      display_name: "GitHub (octocat/Hello-World)",
      description: "Limited to octocat/Hello-World",
      restrict_to_rules: true,
      rules: octocatRules(),
    });
    // The rules the form saved are in it, for a retry.
    expect(screen.getByLabelText("Website")).toHaveValue("api.github.com");
  });

  it("applies them for a custom service on a GitHub host, with the options that were ticked", async () => {
    const { onSave } = await renderEditor();
    type("Website", "api.github.com");
    fireEvent.click(screen.getByLabelText(/Also allow GraphQL/));
    enter("octocat/Hello-World");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("api", {
      display_name: "GitHub (octocat/Hello-World)",
      description: "Limited to octocat/Hello-World",
      restrict_to_rules: true,
      placeholder_env: {
        GH_TOKEN: "mindroom-brokered",
        GITHUB_TOKEN: "mindroom-brokered",
      },
      rules: octocatRules(true),
    });
  });

  it("refuses a line it cannot use instead of saving the broad service, and focuses the repositories", async () => {
    const { onSave } = await renderGithub();
    enter("not a repo");
    save();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "not a repo is not owner/repo or owner/*",
    );
    expect(screen.getByLabelText("GitHub repositories")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("GitHub repositories")).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
    // Typing again takes the mark off, and a valid line then saves.
    enter("octocat/Hello-World");
    expect(screen.queryByRole("alert")).toBeNull();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  it("leaves the service as it is when nothing was typed", async () => {
    const { onSave } = await renderGithub();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", { preset: "github" });
  });

  it("keeps rules edited by hand after applying", async () => {
    const { onSave } = await renderGithub();
    enter("octocat/Hello-World");
    apply();
    type("Rule 3 path prefix", "/octocat/Hello-World.git/");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(vi.mocked(onSave).mock.calls[0][1].rules?.[2].path_prefix).toBe(
      "/octocat/Hello-World.git/",
    );
  });

  it("applies again when the repositories change after applying", async () => {
    const { onSave } = await renderGithub();
    enter("octocat/Hello-World");
    apply();
    // Changing the list is a new choice, so the rules follow it.
    enter("octocat/Hello-World\nmindroom-ai/*");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const rules = vi.mocked(onSave).mock.calls[0][1].rules ?? [];
    expect(rules.map((rule) => rule.path_prefix)).toEqual([
      "/repos/octocat/Hello-World",
      "/octocat/Hello-World",
      "/octocat/Hello-World.git",
      "/repos/mindroom-ai/",
      "/mindroom-ai/",
    ]);
  });

  it("counts typed repositories as unsaved changes", async () => {
    const onDirtyChange = vi.fn();
    await renderGithub({ onDirtyChange });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    enter("octocat/Hello-World");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    enter("");
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });
});

describe("typed repositories after the preset changes", () => {
  it("applies them again on Save when the preset was switched away and back after applying", async () => {
    const { onSave } = await renderGithub();
    enter("octocat/Hello-World");
    apply();
    // The preset's own rules are back, and the repositories are still typed.
    type("Preset", "openai");
    type("Preset", "github");
    expect(screen.getByLabelText("GitHub repositories")).toHaveValue(
      "octocat/Hello-World",
    );
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith(
      "github",
      expect.objectContaining({
        restrict_to_rules: true,
        rules: githubRepositoryRules("octocat/Hello-World", {
          uploads: false,
          graphql: false,
        }).rules,
      }),
    );
  });

  it("does not count text in a helper that is no longer shown as unsaved changes", async () => {
    const onDirtyChange = vi.fn();
    await renderEditor({ onDirtyChange });
    type("Preset", "github");
    enter("octocat/Hello-World");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    // Back to a custom service as the form opened: only the hidden text differs.
    type("Preset", "");
    expect(screen.queryByLabelText("GitHub repositories")).toBeNull();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });
});

/** Wait until the limit form can be saved: the presets, which bring the validation, have loaded or failed. */
const limitSavable = () =>
  waitFor(() =>
    expect(screen.getByRole("button", { name: "Save limit" })).toBeEnabled(),
  );

describe("a limit: a service that only restricts GitHub repositories", () => {
  /** The limit form has no preset, but it waits for the validation the presets bring. */
  const renderLimit = async (props: Partial<EgressServiceEditorProps> = {}) => {
    const merged = { ...baseProps(), ...props };
    render(<EgressServiceEditor {...merged} />);
    await limitSavable();
    return { onSave: merged.onSave, onClose: merged.onClose };
  };

  it("is added with its own title, no preset, website, key, or More options, and a name given", async () => {
    await renderLimit({
      initialRestrictOnly: true,
      saveKey: vi.fn(async () => undefined),
    });
    expect(
      screen.getByRole("form", { name: "Limit to repositories" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("github-limited");
    expect(screen.getByLabelText("GitHub repositories")).toHaveValue("");
    for (const label of ["Preset", "Website", "Send the key as", "API key"])
      expect(screen.queryByLabelText(label)).toBeNull();
    expect(screen.queryByRole("button", { name: "More options" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Use these repositories" }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "Save limit" })).toBeEnabled();
  });

  it("is not unsaved changes until the user changes something", async () => {
    const onDirtyChange = vi.fn();
    await renderLimit({ initialRestrictOnly: true, onDirtyChange });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("takes the first github-limited name that is free", async () => {
    await renderLimit({
      initialRestrictOnly: true,
      takenNames: ["github-limited", "github-limited-2"],
    });
    expect(screen.getByLabelText("Name")).toHaveValue("github-limited-3");
  });

  it("saves restrict-only rules for the repositories, with no key, login, or placeholders", async () => {
    const saveKey = vi.fn(async () => undefined);
    const onClose = vi.fn();
    const { onSave } = await renderLimit({
      initialRestrictOnly: true,
      saveKey,
      onClose,
    });
    enter("octocat/Hello-World");
    // Its uploads.github.com rules are not a choice: without them that host would stay open.
    expect(screen.queryByLabelText(/Also allow uploads.github.com/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("github-limited", {
      display_name: "GitHub (octocat/Hello-World)",
      description: "Limited to octocat/Hello-World",
      rules: limitRules("octocat/Hello-World"),
      restrict_to_rules: true,
      restrict_only: true,
    });
    expect(saveKey).not.toHaveBeenCalled();
  });

  it("asks for a repository when there is none", async () => {
    const { onSave } = await renderLimit({
      initialRestrictOnly: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Enter at least one repository, such as owner/repo",
    );
    expect(screen.getByLabelText("GitHub repositories")).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("waits for the presets, which bring the validation, before it can be saved", async () => {
    const loadPresets = vi.fn(() => new Promise<EgressPresetCatalog>(() => {}));
    render(
      <EgressServiceEditor
        {...baseProps()}
        initialRestrictOnly
        loadPresets={loadPresets}
      />,
    );
    expect(screen.getByRole("button", { name: "Save limit" })).toBeDisabled();
  });

  it("can be saved once the presets fail to load", async () => {
    const { onSave } = await renderLimit({
      initialRestrictOnly: true,
      loadPresets: failingPresets(),
    });
    enter("octocat/Hello-World");
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
  });

  it("says for whom it also limits the administrator's services", async () => {
    await renderLimit({
      initialRestrictOnly: true,
      scope: "shared",
    });
    expect(
      screen.getByText(
        "This also limits services from your administrator on these hosts, for everyone using this agent.",
      ),
    ).toBeVisible();
  });

  it("opens with its repositories and options when it is edited, and keeps them unless they change", async () => {
    const stored: AuthoredEgressService = {
      ...storedLimit("a/b"),
      rules: limitRules("a/b\nc/*", true),
    };
    // The server keeps a limit's rules without auth, and they open and save back the same.
    expect(stored.rules?.every((rule) => rule.auth === undefined)).toBe(true);
    const { onSave } = await renderLimit({
      name: "github-limited",
      service: stored,
      saveKey: vi.fn(async () => undefined),
    });
    expect(
      screen.getByRole("form", { name: "Change limit" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(screen.getByLabelText("GitHub repositories")).toHaveValue(
      "a/b\nc/*",
    );
    expect(screen.queryByLabelText(/Also allow uploads.github.com/)).toBeNull();
    expect(screen.getByLabelText(/Also allow GraphQL/)).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("github-limited", stored);
  });

  const ownRulesLimit = (
    rules: AuthoredEgressService["rules"],
  ): AuthoredEgressService => ({
    rules,
    restrict_to_rules: true,
    restrict_only: true,
  });

  it.each<[string, AuthoredEgressService]>([
    [
      "a path limit on another website",
      ownRulesLimit([
        { host: "www.googleapis.com", path_prefix: "/drive/v3/files" },
      ]),
    ],
    [
      "a path limit stored without restrict_to_rules",
      {
        restrict_only: true,
        rules: [{ host: "www.googleapis.com", path_prefix: "/drive/v3/files" }],
      },
    ],
    [
      "a GitHub limit that names no repository",
      ownRulesLimit([{ host: "api.github.com", path_prefix: "/user" }]),
    ],
    [
      "repositories with a rule the list cannot show",
      storedLimit("a/b", {
        rules: [
          ...limitRules("a/b"),
          { host: "api.github.com", path_prefix: "/user" },
        ],
      }),
    ],
    [
      "repositories with a port the list cannot show",
      storedLimit("a/b", {
        rules: limitRules("a/b").map((rule, index) =>
          index === 0 ? { ...rule, port: 8443 } : rule,
        ),
      }),
    ],
  ])(
    "opens %s in the rule fields instead of the repository list, and saves it unchanged",
    async (_case, stored) => {
      const { onSave } = await renderLimit({
        name: "my-limit",
        service: stored,
      });
      expect(
        screen.getByRole("form", { name: "Change limit" }),
      ).toBeInTheDocument();
      expect(screen.queryByLabelText("GitHub repositories")).toBeNull();
      const [first, second] = stored.rules!;
      expect(screen.getByLabelText("Website")).toHaveValue(first.host);
      // Every rule's Path prefix field has that label; the first rule's comes first.
      expect(screen.getAllByLabelText("Path prefix")[0]).toHaveValue(
        first.path_prefix,
      );
      if (second)
        expect(screen.getByLabelText("Rule 2 host")).toHaveValue(second.host);
      // A limit sends no key, so its rules have no way to send one.
      for (const label of ["Send the key as", "API key", "Preset"])
        expect(screen.queryAllByLabelText(label)).toHaveLength(0);
      expect(screen.queryByLabelText("Rule 2 auth type")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
      expect(onSave).toHaveBeenCalledWith("my-limit", stored);
    },
  );

  describe("while another service of the viewer's has rules on the hosts of a limit in the rule fields", () => {
    const githubLimit = ownRulesLimit([
      { host: "api.github.com", path_prefix: "/repos/a/b" },
      { host: "api.github.com", path_prefix: "/user" },
    ]);
    const own = (
      host: string,
      changes: Partial<EgressCredentialService> = {},
    ) =>
      credentialService({
        name: "my-service",
        display_name: "My Service",
        source: "user",
        rules: [
          {
            host,
            port: null,
            path_prefix: "/",
            auth_type: "bearer" as const,
            auth_name: null,
          },
        ],
        ...changes,
      });
    const refused =
      "Requests to these hosts whose path matches none of these rules are refused.";

    it("notes that it has rules there too, after saying the other paths are refused", async () => {
      await renderLimit({
        name: "my-limit",
        service: githubLimit,
        agentServices: [own("api.github.com")],
      });
      expect(
        screen.getByText(
          `${refused} My Service also has rules on api.github.com. A request is allowed when any of this agent's own services allows it. Your key or connected account stays the same.`,
        ),
      ).toBeVisible();
    });

    it("says nothing about other services when none has rules on its hosts", async () => {
      await renderLimit({
        name: "my-limit",
        service: githubLimit,
        agentServices: [own("example.com")],
      });
      expect(
        screen.getByText(
          `${refused} Your key or connected account stays the same.`,
        ),
      ).toBeVisible();
      expect(screen.queryByText(/also has rules/)).toBeNull();
    });

    it("ignores the limit itself, a limit, and the administrator's services", async () => {
      await renderLimit({
        name: "my-limit",
        service: githubLimit,
        agentServices: [
          own("api.github.com", { name: "my-limit", restrict_only: true }),
          own("api.github.com", { name: "other-limit", restrict_only: true }),
          own("api.github.com", { name: "admin", source: "config" }),
        ],
      });
      expect(screen.queryByText(/also has rules/)).toBeNull();
    });

    it("compares the hosts of the preset for a limit that takes its rules from one", async () => {
      await renderLimit({
        name: "drive-limited",
        service: { preset: "google_drive", restrict_only: true },
        agentServices: [own("www.googleapis.com")],
      });
      expect(
        screen.getByText(
          `${refused} My Service also has rules on www.googleapis.com. A request is allowed when any of this agent's own services allows it. Your key or connected account stays the same.`,
        ),
      ).toBeVisible();
    });
  });

  it("shows a limit that takes its rules from a preset read-only, and saves it back as a preset", async () => {
    const stored: AuthoredEgressService = {
      preset: "google_drive",
      restrict_only: true,
    };
    const onDelete = vi.fn(async () => undefined);
    const { onSave } = await renderLimit({
      name: "drive-limited",
      service: stored,
      onDelete,
    });
    expect(
      screen.getByText(
        "This limit uses the rules of the Google Drive preset, which cover www.googleapis.com/drive/, www.googleapis.com/upload/drive/. They cannot be changed here.",
      ),
    ).toBeVisible();
    for (const label of [
      "GitHub repositories",
      "Website",
      "Path prefix",
      "Send the key as",
    ])
      expect(screen.queryAllByLabelText(label)).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Remove limit" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("drive-limited", stored);
  });

  it("edits the rules of a limit the repository list cannot write, and adds one, without a key", async () => {
    const { onSave } = await renderLimit({
      name: "drive-limited",
      service: ownRulesLimit([
        { host: "www.googleapis.com", path_prefix: "/drive/v3/files" },
      ]),
    });
    type("Path prefix", "/drive/v3/files/abc");
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    type("Rule 2 host", "www.googleapis.com");
    type("Rule 2 path prefix", "/upload/drive/v3/files/abc");
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(
      "drive-limited",
      ownRulesLimit([
        { host: "www.googleapis.com", path_prefix: "/drive/v3/files/abc" },
        {
          host: "www.googleapis.com",
          path_prefix: "/upload/drive/v3/files/abc",
        },
      ]),
    );
  });

  it("checks the rule fields of such a limit before it saves", async () => {
    const { onSave } = await renderLimit({
      name: "drive-limited",
      service: ownRulesLimit([
        { host: "www.googleapis.com", path_prefix: "/drive/v3/files" },
      ]),
    });
    type("Website", "");
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    expect(screen.getByLabelText("Website")).toHaveAccessibleDescription(
      /Host is required/,
    );
    expect(screen.getByLabelText("Website")).toHaveFocus();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("is not unsaved changes until the repositories change", async () => {
    const onDirtyChange = vi.fn();
    await renderLimit({
      name: "github-limited",
      service: storedLimit("a/b"),
      onDirtyChange,
    });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    enter("a/b\nc/d");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
  });

  it("is removed with Remove limit, after asking what it gives back", async () => {
    const onDelete = vi.fn(async () => undefined);
    await renderLimit({
      name: "github-limited",
      service: storedLimit("a/b"),
      onDelete,
      scope: "user_agent",
      agentName: "Mind",
    });
    expect(screen.queryByRole("button", { name: "Delete service" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove limit" }));
    expect(
      screen.getByText(
        /Remove limit\? This removes the limit\. Your Mind can then reach every repository the key or connected account allows\./,
      ),
    ).toBeVisible();
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm remove" }));
    await waitFor(() => expect(onDelete).toHaveBeenCalledTimes(1));
  });

  it("relabels the limit it wrote when a repository is added, and keeps labels the person wrote", async () => {
    const first = await renderLimit({
      name: "github-limited",
      service: storedLimit("a/b"),
    });
    enter("a/b\nc/d");
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(first.onSave).toHaveBeenCalledTimes(1));
    expect(first.onSave).toHaveBeenCalledWith("github-limited", {
      display_name: "GitHub (a/b, c/d)",
      description: "Limited to a/b, c/d",
      rules: limitRules("a/b\nc/d"),
      restrict_to_rules: true,
      restrict_only: true,
    });
    cleanup();
    const second = await renderLimit({
      name: "github-limited",
      service: {
        ...storedLimit("a/b"),
        display_name: "Work repositories",
        description: "What the team uses",
      },
    });
    enter("a/b\nc/d");
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(second.onSave).toHaveBeenCalledTimes(1));
    expect(second.onSave).toHaveBeenCalledWith(
      "github-limited",
      expect.objectContaining({
        display_name: "Work repositories",
        description: "What the team uses",
      }),
    );
  });

  it("tells a list that makes more than 50 rules in the alert, and does not save", async () => {
    const { onSave } = await renderLimit({
      initialRestrictOnly: true,
    });
    enter(Array.from({ length: 17 }, (_, i) => `o/r${i}`).join("\n"));
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A service can have at most 50 rules",
    );
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("the Website field", () => {
  const website = () => screen.getByLabelText("Website") as HTMLInputElement;
  const typeWebsite = (value: string) =>
    fireEvent.change(website(), { target: { value } });

  it("keeps only the host of a pasted address", async () => {
    await renderEditor();
    typeWebsite("https://httpbin.org/anything?x=1#top");
    expect(website()).toHaveValue("httpbin.org");
    // The name follows the host that is left.
    expect(screen.getByLabelText("Name")).toHaveValue("httpbin");
    typeWebsite("http://api.example.com:8443/v1");
    expect(website()).toHaveValue("api.example.com:8443");
    typeWebsite("a.example.com/path");
    expect(website()).toHaveValue("a.example.com");
    typeWebsite("*.example.com");
    expect(website()).toHaveValue("*.example.com");
  });

  it("lets a person type an address letter by letter", async () => {
    await renderEditor();
    for (const typed of ["h", "https:", "https:/", "https://", "https://a.b"])
      typeWebsite(typed);
    expect(website()).toHaveValue("a.b");
  });

  it("keeps the old problem for a port, which the port field is for", async () => {
    await renderEditor();
    typeWebsite("a.example.com:8443");
    save();
    expect(website()).toHaveAccessibleDescription(
      /Host must not contain a port \(use the port field\)/,
    );
  });

  it("moves the path of a pasted address into the Path prefix and says so", async () => {
    const { onSave } = await renderEditor();
    typeWebsite("https://api.example.com/v1/items?page=2#top");
    expect(website()).toHaveValue("api.example.com");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Moved /v1/items to Path prefix",
    );
    openMoreOptions();
    expect(screen.getByLabelText("Path prefix")).toHaveValue("/v1/items");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("api", {
      display_name: "api.example.com",
      rules: [
        {
          host: "api.example.com",
          path_prefix: "/v1/items",
          auth: { type: "bearer" },
        },
      ],
    });
  });

  it("takes the note away with the next change, and moves nothing for a bare slash", async () => {
    await renderEditor();
    typeWebsite("api.example.com/v1");
    expect(screen.getByText("Moved /v1 to Path prefix")).toBeVisible();
    typeWebsite("api.example.org");
    expect(screen.queryByText(/Moved .* to Path prefix/)).toBeNull();
    cleanup();
    await renderEditor();
    typeWebsite("api.example.com/");
    expect(screen.queryByText(/Moved .* to Path prefix/)).toBeNull();
    openMoreOptions();
    expect(screen.getByLabelText("Path prefix")).toHaveValue("");
  });

  it("keeps a Path prefix that is already filled in, and drops the pasted path without a note", async () => {
    await renderEditor();
    openMoreOptions();
    type("Path prefix", "/mine");
    typeWebsite("https://api.example.com/other");
    expect(website()).toHaveValue("api.example.com");
    expect(screen.getByLabelText("Path prefix")).toHaveValue("/mine");
    expect(screen.queryByText(/Moved .* to Path prefix/)).toBeNull();
  });

  it("moves the path of an extra rule's host into that rule's Path prefix", async () => {
    await renderEditor();
    typeWebsite("a.example.com");
    openMoreOptions();
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    type("Rule 2 host", "https://b.example.com/v2");
    expect(screen.getByLabelText("Rule 2 path prefix")).toHaveValue("/v2");
    expect(screen.getByText("Moved /v2 to Path prefix")).toBeVisible();
    expect(screen.getAllByLabelText("Path prefix")[0]).toHaveValue("");
  });

  it("strips an extra rule's host the same way", async () => {
    await renderEditor();
    typeWebsite("a.example.com");
    openMoreOptions();
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    type("Rule 2 host", "https://b.example.com/x");
    expect(screen.getByLabelText("Rule 2 host")).toHaveValue("b.example.com");
  });
});

describe("the display name of a new service", () => {
  it("is the host, exactly as written and not title-cased, while the field is empty", async () => {
    const { onSave } = await renderEditor();
    type("Website", "Echo-Test.example.com");
    openMoreOptions();
    expect(screen.getByLabelText("Display name")).toHaveAccessibleDescription(
      "Leave empty to use echo-test.example.com.",
    );
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(vi.mocked(onSave).mock.calls[0][1].display_name).toBe(
      "echo-test.example.com",
    );
  });

  it("gives way to a display name that is typed", async () => {
    const { onSave } = await renderEditor();
    type("Website", "httpbin.org");
    openMoreOptions();
    type("Display name", "Httpbin");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(vi.mocked(onSave).mock.calls[0][1].display_name).toBe("Httpbin");
  });

  it("leaves a preset with its own name", async () => {
    const { onSave } = await renderEditor();
    type("Preset", "openai");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("openai", { preset: "openai" });
  });

  it("does not give a service that is edited a display name it did not have", async () => {
    const { onSave } = await renderEditor({
      name: "web",
      service: { rules: [bearerRule] },
    });
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("web", { rules: [bearerRule] });
  });

  it("measures an edited service by what its save sends, without the host as display name", async () => {
    const stored = (length: number) => ({
      rules: [bearerRule],
      description: "x".repeat(length),
    });
    // Exactly the server's cap, counting only what the save writes.
    const fits =
      validation.max_service_bytes - JSON.stringify(stored(0)).length;
    const { onSave } = await renderEditor({
      name: "web",
      service: stored(fits),
    });
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("web", stored(fits));
    cleanup();
    // One byte more is still refused.
    const over = await renderEditor({ name: "web", service: stored(fits + 1) });
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A service can take at most 16 KiB",
    );
    expect(over.onSave).not.toHaveBeenCalled();
  });

  it("offers the host only while the service is new: an edited one keeps no display name", async () => {
    await renderEditor({ name: "web", service: { rules: [bearerRule] } });
    openMoreOptions();
    expect(
      screen.getByLabelText("Display name"),
    ).not.toHaveAccessibleDescription(/Leave empty/);
    cleanup();
    // A preset service is still named after its preset when the field is empty.
    await renderEditor({ name: "gh", service: { preset: "github" } });
    openMoreOptions();
    expect(screen.getByLabelText("Display name")).toHaveAccessibleDescription(
      "Leave empty to use GitHub.",
    );
  });
});

describe("a website another service of the agent already sends a key to", () => {
  const sending = (changes: Partial<EgressCredentialService> = {}) =>
    credentialService({
      description: "",
      configured: true,
      active_source: "key",
      key_configured: true,
      rules: [
        {
          host: "api.github.com",
          port: null,
          path_prefix: "/",
          auth_type: "bearer",
          auth_name: null,
        },
        {
          host: "github.com",
          port: null,
          path_prefix: "/",
          auth_type: "basic",
          auth_name: null,
        },
      ],
      ...changes,
    });
  const WARNING =
    " already sends a key to api.github.com for this agent. An exact host wins, then a specific port, then the longest path, then your administrator's services before your own, then the service name alphabetically.";

  it("warns, naming the service and the website, as soon as the website is typed", async () => {
    await renderEditor({ agentServices: [sending()] });
    expect(screen.queryByText(/already sends a key/)).toBeNull();
    type("Website", "api.github.com");
    expect(screen.getByText(`GitHub${WARNING}`)).toBeVisible();
    type("Website", "API.example.com");
    expect(screen.queryByText(/already sends a key/)).toBeNull();
  });

  it("is announced as a status, not an alert, since the service can still be saved", async () => {
    await renderEditor({ agentServices: [sending()] });
    type("Website", "api.github.com");
    expect(screen.getByText(`GitHub${WARNING}`)).toHaveAttribute(
      "role",
      "status",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("names every website they share once, for one service", async () => {
    await renderEditor({ agentServices: [sending()] });
    type("Preset", "github");
    expect(
      screen.getByText(
        "GitHub already sends a key to api.github.com and github.com for this agent. An exact host wins, then a specific port, then the longest path, then your administrator's services before your own, then the service name alphabetically.",
      ),
    ).toBeVisible();
  });

  it("is a warning only: the service still saves", async () => {
    const { onSave } = await renderEditor({ agentServices: [sending()] });
    type("Website", "api.github.com");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  it("ignores a service that only limits paths, one that sends no key there, and the service being edited", async () => {
    await renderEditor({
      agentServices: [
        sending({ name: "limit", display_name: "Limit", restrict_only: true }),
        sending({
          name: "nokey",
          display_name: "NoKey",
          rules: [
            {
              host: "api.github.com",
              port: null,
              path_prefix: "/",
              auth_type: null,
              auth_name: null,
            },
          ],
        }),
      ],
    });
    type("Website", "api.github.com");
    expect(screen.queryByText(/already sends a key/)).toBeNull();
    cleanup();
    await renderGithub({ agentServices: [sending()] });
    expect(screen.queryByText(/already sends a key/)).toBeNull();
  });

  it("says nothing on the dashboard, which has no agent", async () => {
    await renderEditor({ context: "operator" });
    type("Website", "api.github.com");
    expect(screen.queryByText(/already sends a key/)).toBeNull();
  });
});
