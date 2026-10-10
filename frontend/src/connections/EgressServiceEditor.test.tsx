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
  DiscardEditsNotice,
  EgressServiceEditor,
  type EgressServiceEditorProps,
} from "./EgressServiceEditor";
import type { EgressPreset, EgressRule } from "./types";

// The JSON fixture types each placeholder_env by its own keys.
const presets = egressPresets.presets as EgressPreset[];

const bearerRule: EgressRule = {
  host: "api.example.com",
  auth: { type: "bearer" },
};

const baseProps = () => ({
  service: null,
  context: "personal" as const,
  loadPresets: vi.fn(async () => presets),
  onSave: vi.fn(async () => undefined),
  onCancel: vi.fn(),
});

/** Render the editor and wait until the server's presets are in the dropdown. */
async function renderEditor(props: Partial<EgressServiceEditorProps> = {}) {
  const merged = { ...baseProps(), ...props };
  render(<EgressServiceEditor {...merged} />);
  await waitFor(() => expect(screen.getByLabelText("Preset")).toBeEnabled());
  return { onSave: merged.onSave, onCancel: merged.onCancel };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("the editor form", () => {
  const type = (label: string, value: string) =>
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  const save = () =>
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

  it("saves a custom service in its authored form", async () => {
    const { onSave } = await renderEditor();
    type("Name", "example");
    type("Display name", "Example");
    type("Rule 1 host", "API.example.com");
    type("Rule 1 path prefix", "/v1/");
    fireEvent.change(screen.getByLabelText("Rule 1 auth type"), {
      target: { value: "header" },
    });
    type("Rule 1 header name", "x-api-key");
    type("Rule 1 template", "Key {secret}");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("example", {
      display_name: "Example",
      rules: [
        {
          host: "api.example.com",
          path_prefix: "/v1/",
          auth: { type: "header", name: "x-api-key", template: "Key {secret}" },
        },
      ],
    });
  });

  it("adds, fills, and removes rules", async () => {
    const { onSave } = await renderEditor();
    type("Name", "two");
    type("Rule 1 host", "a.example.com");
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    type("Rule 2 host", "b.example.com");
    fireEvent.change(screen.getByLabelText("Rule 2 auth type"), {
      target: { value: "basic" },
    });
    type("Rule 2 username", "bot");
    fireEvent.click(screen.getByRole("button", { name: "Remove Rule 1" }));
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("two", {
      rules: [
        { host: "b.example.com", auth: { type: "basic", username: "bot" } },
      ],
    });
  });

  it("saves a preset alone as just the preset", async () => {
    const { onSave } = await renderEditor();
    type("Name", "github");
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
    expect(
      screen.getByText(
        "The GitHub preset covers api.github.com, uploads.github.com, github.com.",
      ),
    ).toBeInTheDocument();
    // The preset supplies the rules, so no rule fields are shown.
    expect(screen.queryByLabelText("Rule 1 host")).toBeNull();
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", { preset: "github" });
  });

  it("saves a preset with restrict_to_rules without expanding it", async () => {
    const { onSave } = await renderEditor();
    type("Name", "github");
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
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
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    fireEvent.click(
      screen.getByLabelText("Replace the preset's rules with my own"),
    );
    type("Rule 1 host", "proxy.example.com");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("ai", {
      preset: "openai",
      rules: [{ host: "proxy.example.com", auth: { type: "bearer" } }],
    });
  });

  it("explains the GraphQL caveat next to the toggle", async () => {
    await renderEditor();
    expect(
      screen.getByText(
        /a listed GraphQL rule reaches every repository the key can\s+reach/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/fine-grained GitHub token/)).toBeInTheDocument();
  });

  it("shows what is wrong instead of saving an invalid form", async () => {
    const { onSave } = await renderEditor();
    type("Name", "Bad Name");
    save();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Name must start with a lowercase");
    expect(alert).toHaveTextContent("Rule 1: host is required");
    expect(onSave).not.toHaveBeenCalled();
    // The messages follow the form once the user has tried to save.
    type("Name", "good");
    type("Rule 1 host", "a.example.com");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("rejects a name that is already taken", async () => {
    const { onSave } = await renderEditor({ takenNames: ["github"] });
    type("Name", "github");
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A service named github already exists",
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows the service's own error and stays open", async () => {
    const onSave = vi.fn(async () => {
      throw new Error("service name 'x' is already used by your administrator");
    });
    await renderEditor({ onSave });
    type("Name", "x");
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already used by your administrator",
    );
    expect(screen.getByRole("button", { name: "Save service" })).toBeEnabled();
  });

  it("cannot rename the service it edits", async () => {
    await renderEditor({ name: "github", service: { preset: "github" } });
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

  it("shows the placeholder rules as hints in the advanced section", async () => {
    await renderEditor();
    expect(screen.queryByText(/must be TOKEN, KEY/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    const hint = screen.getByText(/one word between underscores must be/);
    expect(hint).toHaveTextContent(
      "No word may be COMMAND, CMD, FILE, DIR, PROVIDER, HELPER, OPTS, OPTIONS, or EXTENSIONS",
    );
    expect(hint).toHaveTextContent(
      "names cannot start with SSH_, MINDROOM_, or GIT_CONFIG_",
    );
    expect(hint).toHaveTextContent(
      "PATH, HOME, and proxy and CA variables are reserved",
    );
  });

  it("refuses a placeholder the server would, and says why", async () => {
    const { onSave } = await renderEditor();
    type("Name", "svc");
    type("Rule 1 host", "a.example.com");
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    fireEvent.click(screen.getByRole("button", { name: "Add placeholder" }));
    type("Placeholder 1 name", "SSH_AUTH_SOCK");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "placeholder name SSH_AUTH_SOCK may not start with SSH_",
    );
    type("Placeholder 1 name", "MINDROOM_API_KEY");
    save();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Placeholder name MINDROOM_API_KEY starts with reserved prefix MINDROOM_",
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("shows the operator's placeholder rules instead on the dashboard", async () => {
    await renderEditor({ context: "operator" });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(
      screen.getByText(/MINDROOM_ or GIT_CONFIG_ are reserved/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/must be TOKEN, KEY/)).toBeNull();
  });

  it("adds a placeholder with the brokered value and saves it", async () => {
    const { onSave } = await renderEditor();
    type("Name", "svc");
    type("Rule 1 host", "a.example.com");
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    fireEvent.click(screen.getByRole("button", { name: "Add placeholder" }));
    expect(screen.getByLabelText("Placeholder 1 value")).toHaveValue(
      "mindroom-brokered",
    );
    type("Placeholder 1 name", "SVC_API_KEY");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("svc", {
      rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
      placeholder_env: { SVC_API_KEY: "mindroom-brokered" },
    });
  });

  it("opens the advanced section when the service already has placeholders", async () => {
    await renderEditor({
      name: "svc",
      service: { preset: "github", placeholder_env: { GH_TOKEN: "x" } },
    });
    expect(screen.getByLabelText("Placeholder 1 name")).toHaveValue("GH_TOKEN");
  });

  it("says a personal service applies to all personal agents of the same scope", async () => {
    const { rerender } = render(<EgressServiceEditor {...baseProps()} />);
    const note =
      "This service applies to all of your personal agents that share this scope.";
    expect(screen.getByText(note)).toBeInTheDocument();
    // A shared agent's service is everyone's, and the dashboard edits config.yaml.
    rerender(<EgressServiceEditor {...baseProps()} sharedTarget />);
    expect(screen.queryByText(note)).toBeNull();
    rerender(<EgressServiceEditor {...baseProps()} context="operator" />);
    expect(screen.queryByText(note)).toBeNull();
  });

  it("disables the account login on a shared agent", async () => {
    const { onSave } = await renderEditor({ sharedTarget: true });
    expect(screen.getByLabelText("Account login (optional)")).toBeDisabled();
    expect(
      screen.getByText(
        "Accounts cannot be connected on a shared agent. Use an API key.",
      ),
    ).toBeInTheDocument();
    type("Name", "github");
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
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
    type("Rule 1 host", "a.example.com");
    type("Account login (optional)", "atlassian");
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("svc", {
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
    await renderEditor({
      name: "github",
      service: { preset: "github" },
      onDelete,
    });
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
    const { onSave, onCancel } = await renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("the GitHub helper in the editor", () => {
  const renderGithub = (props: Partial<EgressServiceEditorProps> = {}) =>
    renderEditor({ service: { preset: "github" }, name: "github", ...props });
  const enter = (value: string) =>
    fireEvent.change(screen.getByLabelText("GitHub repositories"), {
      target: { value },
    });
  const apply = () =>
    fireEvent.click(
      screen.getByRole("button", { name: "Use these repositories" }),
    );
  const save = () =>
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

  it("replaces the preset's rules and turns on the restriction", async () => {
    const { onSave } = await renderGithub();
    enter("basnijholt/agent-cli");
    apply();
    expect(screen.getByLabelText("Rule 1 host")).toHaveValue("api.github.com");
    expect(screen.getByLabelText("Rule 1 path prefix")).toHaveValue(
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

  it("makes owner-wide rules and leaves uploads and GraphQL off by default", async () => {
    await renderGithub();
    expect(
      screen.getByLabelText(/Also allow uploads.github.com/),
    ).not.toBeChecked();
    expect(screen.getByLabelText(/Also allow GraphQL/)).not.toBeChecked();
    enter("mindroom-ai/*");
    apply();
    expect(screen.getByLabelText("Rule 1 path prefix")).toHaveValue(
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
    expect(screen.queryByLabelText("Rule 1 host")).toBeNull();
  });

  it("is offered for a new custom service and for rules on GitHub hosts only", async () => {
    cleanup();
    await renderEditor({ name: undefined, service: null });
    expect(screen.getByText("Limit to repositories")).toBeInTheDocument();
    cleanup();
    await renderEditor({ name: "openai", service: { preset: "openai" } });
    expect(screen.queryByText("Limit to repositories")).toBeNull();
    cleanup();
    await renderEditor({ name: "code", service: { rules: [bearerRule] } });
    expect(screen.queryByText("Limit to repositories")).toBeNull();
    cleanup();
    await renderEditor({
      name: "code",
      service: {
        rules: [{ host: "github.com", auth: { type: "bearer" } }],
      },
    });
    expect(screen.getByText("Limit to repositories")).toBeInTheDocument();
  });
});

describe("the broader-rule warning", () => {
  const covering = (
    display_name: string,
    path_prefix: string,
    host = "api.github.com",
  ) => ({
    name: display_name.toLowerCase(),
    display_name,
    rules: [{ host, port: null, path_prefix }],
  });
  const restrict = () =>
    fireEvent.click(screen.getByLabelText("Refuse other paths on these hosts"));
  const note = () => screen.queryByRole("note");

  it("names a service that covers a limited repository more widely", async () => {
    await renderEditor({
      service: { preset: "github" },
      name: "limited",
      otherServices: [covering("GitHub", "/")],
    });
    fireEvent.change(screen.getByLabelText("GitHub repositories"), {
      target: { value: "o/r" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Use these repositories" }),
    );

    expect(note()).toHaveTextContent(
      "A broader rule of GitHub on the same host already covers some of these paths. Refusing other paths cannot narrow it",
    );
    // It sits by the restriction it is about.
    expect(
      within(
        screen
          .getByLabelText("Refuse other paths on these hosts")
          .closest("div") as HTMLElement,
      ).getByRole("note"),
    ).toBe(note());
  });

  it("appears only while the restriction is on", async () => {
    await renderEditor({
      service: {
        rules: [
          {
            host: "api.github.com",
            path_prefix: "/repos/o/r",
            auth: { type: "bearer" },
          },
        ],
      },
      name: "limited",
      otherServices: [covering("GitHub", "/")],
    });
    expect(note()).toBeNull();
    restrict();
    expect(note()).not.toBeNull();
    restrict();
    expect(note()).toBeNull();
  });

  it("stays quiet without a shorter covering rule on the same host", async () => {
    await renderEditor({
      service: {
        restrict_to_rules: true,
        rules: [
          {
            host: "api.github.com",
            path_prefix: "/repos/o/r",
            auth: { type: "bearer" },
          },
        ],
      },
      name: "limited",
      otherServices: [
        covering("Same", "/repos/o/r"),
        covering("Longer", "/repos/o/r/issues"),
        covering("Elsewhere", "/", "github.com"),
        covering("Sibling", "/repos/other/"),
        { name: "old", display_name: "Old" },
      ],
    });
    expect(note()).toBeNull();
  });

  it("ignores the service being edited", async () => {
    await renderEditor({
      service: {
        restrict_to_rules: true,
        rules: [
          {
            host: "api.github.com",
            path_prefix: "/repos/o/r",
            auth: { type: "bearer" },
          },
        ],
      },
      name: "github",
      otherServices: [covering("GitHub", "/")],
    });
    expect(note()).toBeNull();
  });

  it("uses the preset's own rules while it keeps them", async () => {
    await renderEditor({
      service: { preset: "google_drive", restrict_to_rules: true },
      name: "drive",
      otherServices: [covering("Google", "/", "www.googleapis.com")],
    });
    // The preset's `/drive/` is narrower than the other service's `/`.
    expect(note()).toHaveTextContent("A broader rule of Google");
  });

  it("names several services together", async () => {
    await renderEditor({
      service: {
        restrict_to_rules: true,
        rules: [
          {
            host: "api.github.com",
            path_prefix: "/repos/o/r",
            auth: { type: "bearer" },
          },
        ],
      },
      name: "limited",
      otherServices: [covering("GitHub", "/"), covering("Owner", "/repos/o/")],
    });
    expect(note()).toHaveTextContent("A broader rule of GitHub and Owner");
  });
});

describe("loading the presets", () => {
  it("offers what the server returns, by display name", async () => {
    await renderEditor({
      loadPresets: vi.fn(async () => [
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
      ]),
    });
    expect(
      within(screen.getByLabelText("Preset"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Custom", "Internal API"]);
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "internal" },
    });
    expect(
      screen.getByText(
        "The Internal API preset covers api.internal.example.com:8443/v1/.",
      ),
    ).toBeInTheDocument();
  });

  it("waits for the presets before the form can be saved", async () => {
    let resolve: (presets: EgressPreset[]) => void = () => {};
    const loadPresets = vi.fn(
      () =>
        new Promise<EgressPreset[]>((done) => {
          resolve = done;
        }),
    );
    render(<EgressServiceEditor {...baseProps()} loadPresets={loadPresets} />);
    expect(screen.getByLabelText("Preset")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save service" })).toBeDisabled();
    resolve(presets);
    await waitFor(() => expect(screen.getByLabelText("Preset")).toBeEnabled());
    expect(screen.getByRole("button", { name: "Save service" })).toBeEnabled();
    expect(loadPresets).toHaveBeenCalledTimes(1);
  });

  it("falls back to a custom service when they cannot be loaded", async () => {
    const onSave = vi.fn(async () => undefined);
    await renderEditor({
      loadPresets: vi.fn(async () => {
        throw new Error("nope");
      }),
      onSave,
    });
    expect(
      screen.getByText(/The preset list could not be loaded/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "own" },
    });
    fireEvent.change(screen.getByLabelText("Rule 1 host"), {
      target: { value: "a.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("own", {
      rules: [{ host: "a.example.com", auth: { type: "bearer" } }],
    });
  });

  it("keeps a preset the server no longer lists", async () => {
    await renderEditor({
      name: "old",
      service: { preset: "retired" },
      loadPresets: vi.fn(async () => []),
    });
    expect(screen.getByLabelText("Preset")).toHaveValue("retired");
  });
});

describe("saving with a wider effect", () => {
  const saveWarning = "This also saves your other unsaved settings changes.";
  const filled = async (props: Partial<EgressServiceEditorProps> = {}) => {
    const rendered = await renderEditor({ saveWarning, ...props });
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "svc" },
    });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    return rendered;
  };
  const save = () =>
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

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
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "changed" },
    });
    expect(screen.queryByText(saveWarning)).toBeNull();
    save();
    expect(screen.getByText(saveWarning)).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not ask for an invalid form", async () => {
    await renderEditor({ saveWarning });
    save();
    expect(screen.getByRole("alert")).toHaveTextContent("Name must start");
    expect(screen.queryByText(saveWarning)).toBeNull();
  });

  it("saves straight away without a warning", async () => {
    const { onSave } = await filled({ saveWarning: null });
    save();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  it("repeats the warning when asking to delete", async () => {
    await renderEditor({
      name: "github",
      service: { preset: "github" },
      onDelete: vi.fn(),
      saveWarning,
    });
    fireEvent.click(screen.getByRole("button", { name: "Delete service" }));
    expect(
      screen.getByText(`Delete github? ${saveWarning}`),
    ).toBeInTheDocument();
  });
});

describe("reporting unsaved changes", () => {
  it("tells the parent when the form differs from what it opened with", async () => {
    const onDirtyChange = vi.fn();
    await renderEditor({
      name: "github",
      service: { preset: "github" },
      onDirtyChange,
    });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "x" },
    });
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "" },
    });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("says it is clean again when it closes", async () => {
    const onDirtyChange = vi.fn();
    await renderEditor({ onDirtyChange });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    cleanup();
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
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(
      screen.getByText(
        /This service sets none, which drops the placeholders of the GitHub preset/,
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("github", {
      preset: "github",
      description: "",
      placeholder_env: {},
    });
  });

  it("offers the preset's placeholders when the service has not set any", async () => {
    await renderEditor({ name: "github", service: { preset: "github" } });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
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
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    // The GitHub preset has placeholders of its own, so the list is not "none".
    expect(
      screen.getByText(
        /Leave the list empty to use the placeholders of the GitHub preset/,
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("mine", { preset: "github" });
  });
});

describe("the GitHub helper on a custom service", () => {
  const enter = (value: string) =>
    fireEvent.change(screen.getByLabelText("GitHub repositories"), {
      target: { value },
    });
  const apply = () =>
    fireEvent.click(
      screen.getByRole("button", { name: "Use these repositories" }),
    );
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
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "code" },
    });
    expect(screen.queryByLabelText("Placeholder 1 name")).toBeNull();
    enter("o/r");
    apply();

    // The advanced section opens so the user sees what was added.
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
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("code", {
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
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledWith("code", {
      rules: rulesFor("o/r"),
      restrict_to_rules: true,
      placeholder_env: { MY_GITHUB_TOKEN: "mindroom-brokered" },
    });
  });

  it("adds them only once when the repositories are applied again", async () => {
    await renderEditor();
    enter("o/r");
    apply();
    enter("o/other");
    apply();
    expect(screen.getByLabelText("Placeholder 2 name")).toHaveValue(
      "GITHUB_TOKEN",
    );
    expect(screen.queryByLabelText("Placeholder 3 name")).toBeNull();
  });

  it("adds none to a preset service, which brings its own", async () => {
    const { onSave } = await renderEditor({
      name: "github",
      service: { preset: "github" },
    });
    enter("o/r");
    apply();
    expect(screen.queryByLabelText("Placeholder 1 name")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
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
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "svc" },
    });
    fireEvent.change(screen.getByLabelText("Rule 1 host"), {
      target: { value: "a.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server said no",
    );
    expect(screen.getByLabelText("Name")).toHaveValue("svc");
    expect(screen.getByLabelText("Rule 1 host")).toHaveValue("a.example.com");
    expect(screen.getByRole("button", { name: "Save service" })).toBeEnabled();

    fireEvent.change(screen.getByLabelText("Rule 1 host"), {
      target: { value: "b.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    fireEvent.change(screen.getByLabelText("Rule 2 host"), {
      target: { value: "c.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(onSave).toHaveBeenLastCalledWith("svc", {
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
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "svc" },
    });
    fireEvent.change(screen.getByLabelText("Rule 1 host"), {
      target: { value: "a.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    await screen.findByText("The server said no");

    fireEvent.change(screen.getByLabelText("Rule 1 host"), {
      target: { value: "https://a.example.com" },
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Rule 1: host must not contain a scheme",
    );
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});

describe("the account login field", () => {
  it("refuses a value that is not a provider id", async () => {
    const { onSave } = await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "svc" },
    });
    fireEvent.change(screen.getByLabelText("Rule 1 host"), {
      target: { value: "a.example.com" },
    });
    fireEvent.change(screen.getByLabelText("Account login (optional)"), {
      target: { value: "Git Hub" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Account login must be a provider id",
    );
    expect(onSave).not.toHaveBeenCalled();
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
