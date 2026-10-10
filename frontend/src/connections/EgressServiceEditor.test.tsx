import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EgressServiceEditor,
  type EgressServiceEditorProps,
  findBroaderGithubService,
  formFromService,
  githubRepositoryRules,
  serializeService,
  validateService,
} from "./EgressServiceEditor";
import type {
  AuthoredEgressService,
  EgressCredentialService,
  EgressRule,
} from "./types";

type Options = Parameters<typeof serializeService>[1];

const personal = { context: "personal", sharedTarget: false } as const;
const sharedPersonal = { context: "personal", sharedTarget: true } as const;
const operator = { context: "operator", sharedTarget: false } as const;
const newService = { editing: false, takenNames: [] as string[] };

const bearerRule: EgressRule = {
  host: "api.example.com",
  auth: { type: "bearer" },
};

const serialize = (
  service: AuthoredEgressService,
  options: Options = personal,
  name = "svc",
) => serializeService(formFromService(name, service), options, service);

const errorsFor = (
  service: AuthoredEgressService | null,
  changes: Partial<ReturnType<typeof formFromService>> = {},
  options: Options = personal,
  settings: { editing: boolean; takenNames: string[] } = newService,
) =>
  validateService(
    { ...formFromService("svc", service), ...changes },
    options,
    settings,
  );

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("serializing a service", () => {
  it("writes a custom service with only the fields that matter", () => {
    expect(
      serialize({
        display_name: "Example",
        description: " Example API ",
        rules: [
          {
            host: " API.Example.com ",
            port: 8443,
            path_prefix: "/v1/",
            auth: { type: "bearer" },
          },
          { host: "git.example.com", path_prefix: "/", auth: basicAuth("x") },
          {
            host: "b.example.com",
            auth: { type: "header", name: "x-api-key", template: "{secret}" },
          },
          {
            host: "c.example.com",
            auth: { type: "query", name: "key", template: "k={secret}" },
          },
        ],
        placeholder_env: { EXAMPLE_API_KEY: "mindroom-brokered" },
        oauth_provider: "github",
      }),
    ).toEqual({
      display_name: "Example",
      description: "Example API",
      rules: [
        {
          host: "api.example.com",
          port: 8443,
          path_prefix: "/v1/",
          auth: { type: "bearer" },
        },
        { host: "git.example.com", auth: { type: "basic", username: "x" } },
        { host: "b.example.com", auth: { type: "header", name: "x-api-key" } },
        {
          host: "c.example.com",
          auth: { type: "query", name: "key", template: "k={secret}" },
        },
      ],
      placeholder_env: { EXAMPLE_API_KEY: "mindroom-brokered" },
      oauth_provider: "github",
    });
  });

  it("keeps a preset as the preset, without its expanded fields", () => {
    expect(serialize({ preset: "github" })).toEqual({ preset: "github" });
    expect(serialize({ preset: "openai" }, operator)).toEqual({
      preset: "openai",
    });
  });

  it("adds only what the user changed to a preset", () => {
    expect(serialize({ preset: "github", restrict_to_rules: true })).toEqual({
      preset: "github",
      restrict_to_rules: true,
    });
    expect(
      serialize({ preset: "github", display_name: "GitHub work" }),
    ).toEqual({ preset: "github", display_name: "GitHub work" });
    expect(serialize({ preset: "github", rules: [bearerRule] })).toEqual({
      preset: "github",
      rules: [bearerRule],
    });
  });

  it("never writes restrict_to_rules false or empty fields", () => {
    expect(
      serialize({
        rules: [bearerRule],
        restrict_to_rules: false,
        description: "",
        placeholder_env: {},
      }),
    ).toEqual({ rules: [bearerRule] });
  });

  it("drops the preset's account login on a shared agent", () => {
    expect(serialize({ preset: "github" }, sharedPersonal)).toEqual({
      preset: "github",
      oauth_provider: null,
    });
    // Presets without a login need nothing, and personal agents keep it.
    expect(serialize({ preset: "openai" }, sharedPersonal)).toEqual({
      preset: "openai",
    });
    expect(serialize({ preset: "github" })).toEqual({ preset: "github" });
  });

  it("never writes an account login for a custom service on a shared agent", () => {
    expect(
      serialize(
        { rules: [bearerRule], oauth_provider: "github" },
        sharedPersonal,
      ),
    ).toEqual({ rules: [bearerRule] });
  });

  it("keeps an operator's oauth_on_shared_workers and never sends it from the personal page", () => {
    const service = {
      preset: "github",
      oauth_on_shared_workers: true,
    } satisfies AuthoredEgressService;
    expect(serialize(service, operator)).toEqual(service);
    expect(serialize(service, personal)).toEqual({ preset: "github" });
  });

  it("keeps an explicit OAuth override of a preset", () => {
    expect(
      serialize({ preset: "github", oauth_provider: null }, operator),
    ).toEqual({ preset: "github", oauth_provider: null });
  });

  it("round-trips an authored service through the form", () => {
    const service: AuthoredEgressService = {
      preset: "github",
      restrict_to_rules: true,
      rules: [
        {
          host: "api.github.com",
          path_prefix: "/repos/o/r",
          auth: { type: "bearer" },
        },
      ],
      placeholder_env: { GH_TOKEN: "mindroom-brokered" },
    };
    expect(serialize(service, operator)).toEqual(service);
  });
});

function basicAuth(username: string) {
  return { type: "basic", username } as const;
}

describe("validating a service", () => {
  it("accepts a preset alone and a valid custom service", () => {
    expect(errorsFor({ preset: "github" })).toEqual([]);
    expect(errorsFor(null, { name: "svc", rules: [validRule()] })).toEqual([]);
  });

  it("checks the name of a new service only", () => {
    expect(errorsFor({ preset: "github" }, { name: "" })[0]).toContain(
      "Name must start",
    );
    expect(errorsFor({ preset: "github" }, { name: "Has Caps" })[0]).toContain(
      "Name must start",
    );
    expect(errorsFor({ preset: "github" }, { name: "_hidden" })[0]).toContain(
      "Name must start",
    );
    expect(errorsFor({ preset: "github" }, { name: "x_oauth" })).toEqual([
      "Name must not end in _oauth or _oauth_client",
    ]);
    expect(
      errorsFor({ preset: "github" }, { name: "github" }, personal, {
        editing: false,
        takenNames: ["github"],
      }),
    ).toEqual(["A service named github already exists"]);
    // An existing service keeps its name, even one that is taken by itself.
    expect(
      errorsFor({ preset: "github" }, { name: "github" }, personal, {
        editing: true,
        takenNames: ["github"],
      }),
    ).toEqual([]);
  });

  it("requires a rule unless a preset supplies them", () => {
    expect(errorsFor(null, { rules: [] })).toEqual(["Add at least one rule"]);
    expect(
      errorsFor({ preset: "github" }, { replacePresetRules: true, rules: [] }),
    ).toEqual(["Add at least one rule"]);
    expect(errorsFor({ preset: "github" }, { rules: [] })).toEqual([]);
  });

  it.each([
    ["", "Rule 1: host is required"],
    ["https://api.example.com", "Rule 1: host must not contain a scheme"],
    ["api.example.com:8443", "Rule 1: host must not contain a port"],
    ["api.example.com/v1", "Rule 1: host must not contain a path"],
    ["*.*.example.com", "Rule 1: host can only have one wildcard label"],
    ["*.com", "Rule 1: wildcard host must have at least one domain part"],
    ["api.*.com", "Rule 1: wildcard must be a single leading *. label"],
  ])("rejects the host %j", (host, message) => {
    const errors = errorsFor(null, { rules: [{ ...validRule(), host }] });
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain(message);
  });

  it("accepts a wildcard host and an IP-less plain host", () => {
    expect(
      errorsFor(null, { rules: [{ ...validRule(), host: "*.example.com" }] }),
    ).toEqual([]);
  });

  it("checks path prefix and port", () => {
    expect(
      errorsFor(null, { rules: [{ ...validRule(), pathPrefix: "v1" }] }),
    ).toEqual(["Rule 1: path prefix must start with /"]);
    for (const port of ["0", "70000", "abc", "1.5"])
      expect(errorsFor(null, { rules: [{ ...validRule(), port }] })).toEqual([
        "Rule 1: port must be a number from 1 to 65535",
      ]);
    expect(
      errorsFor(null, { rules: [{ ...validRule(), port: "8443" }] }),
    ).toEqual([]);
  });

  it("checks the fields each auth type needs", () => {
    const check = (changes: object) =>
      errorsFor(null, { rules: [{ ...validRule(), ...changes }] });
    expect(check({ authType: "basic", username: " " })).toEqual([
      "Rule 1: basic auth needs a username",
    ]);
    expect(check({ authType: "header" })).toEqual([
      "Rule 1: header auth needs a header name",
    ]);
    expect(check({ authType: "header", headerOrParam: "bad header" })).toEqual([
      "Rule 1: header name must be a valid HTTP header name",
    ]);
    expect(check({ authType: "query" })).toEqual([
      "Rule 1: query auth needs a parameter name",
    ]);
    expect(
      check({ authType: "header", headerOrParam: "x-key", template: "token" }),
    ).toEqual(["Rule 1: template must contain {secret} exactly once"]);
    expect(
      check({
        authType: "query",
        headerOrParam: "key",
        template: "{secret}{secret}",
      }),
    ).toEqual(["Rule 1: template must contain {secret} exactly once"]);
    expect(
      check({
        authType: "header",
        headerOrParam: "Authorization",
        template: "Bearer {secret}",
      }),
    ).toEqual([]);
  });

  it("numbers the rule that has the problem", () => {
    expect(
      errorsFor(null, { rules: [validRule(), { ...validRule(), host: "" }] }),
    ).toEqual(["Rule 2: host is required"]);
  });

  it("limits a personal service to 50 rules but not an operator service", () => {
    const rules = Array.from({ length: 51 }, validRule);
    expect(errorsFor(null, { rules })).toEqual([
      "A service can have at most 50 rules",
    ]);
    expect(errorsFor(null, { rules }, operator)).toEqual([]);
  });

  it("limits a personal service to 16 KiB", () => {
    const description = "x".repeat(17 * 1024);
    expect(errorsFor(null, { rules: [validRule()], description })).toEqual([
      "A service can take at most 16 KiB",
    ]);
    expect(
      errorsFor(null, { rules: [validRule()], description }, operator),
    ).toEqual([]);
  });

  it("applies the personal placeholder rules to personal services only", () => {
    const placeholders = (name: string, value = "mindroom-brokered") => ({
      placeholders: [{ name, value }],
    });
    expect(errorsFor({ preset: "github" }, placeholders("MY_API_KEY"))).toEqual(
      [],
    );
    expect(
      errorsFor({ preset: "github" }, placeholders("LD_PRELOAD"))[0],
    ).toContain(
      "must match ^[A-Z][A-Z0-9_]*$ and have one of AUTH, CREDENTIAL, KEY",
    );
    // A credential word has to be a whole part: PYTHONPATH only contains PAT.
    expect(
      errorsFor({ preset: "github" }, placeholders("PYTHONPATH")),
    ).toHaveLength(1);
    expect(errorsFor({ preset: "github" }, placeholders("MY_PAT"))).toEqual([]);
    expect(
      errorsFor(
        { preset: "github" },
        placeholders("MY_API_KEY", "has space"),
      )[0],
    ).toContain("placeholder value of MY_API_KEY must be 1 to 256 characters");
    expect(
      errorsFor({ preset: "github" }, placeholders("MY_API_KEY", "")),
    ).toHaveLength(1);
    // The operator may use any valid variable name and any value.
    expect(
      errorsFor(
        { preset: "github" },
        placeholders("LD_PRELOAD", "a b"),
        operator,
      ),
    ).toEqual([]);
  });

  it("rejects badly shaped and repeated placeholder names, and skips blank rows", () => {
    const rows = (...names: string[]) => ({
      placeholders: names.map((name) => ({ name, value: "mindroom-brokered" })),
    });
    expect(errorsFor({ preset: "github" }, rows("my_key"), operator)).toEqual([
      "Placeholder name my_key must match ^[A-Z_][A-Z0-9_]*$",
    ]);
    expect(
      errorsFor({ preset: "github" }, rows("A_KEY", "A_KEY"), operator),
    ).toEqual(["Placeholder A_KEY is listed twice"]);
    expect(
      errorsFor(
        { preset: "github" },
        { placeholders: [{ name: "", value: "" }] },
        operator,
      ),
    ).toEqual([]);
    expect(
      serializeService(
        {
          ...formFromService("svc", { preset: "github" }),
          placeholders: [{ name: " ", value: "" }],
        },
        operator,
      ),
    ).toEqual({ preset: "github" });
  });
});

function validRule() {
  return {
    host: "api.example.com",
    port: "",
    pathPrefix: "",
    authType: "bearer" as const,
    username: "",
    headerOrParam: "",
    template: "",
  };
}

describe("GitHub repository rules", () => {
  const none = { uploads: false, graphql: false };

  it("generates the API and git rules for one repository", () => {
    expect(githubRepositoryRules("basnijholt/agent-cli", none)).toEqual({
      errors: [],
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

  it("makes an owner-wide rule with a trailing slash for owner/*", () => {
    expect(githubRepositoryRules("mindroom-ai/*", none)).toEqual({
      errors: [],
      rules: [
        {
          host: "api.github.com",
          path_prefix: "/repos/mindroom-ai/",
          auth: { type: "bearer" },
        },
        {
          host: "github.com",
          path_prefix: "/mindroom-ai/",
          auth: { type: "basic", username: "x-access-token" },
        },
      ],
    });
  });

  it("adds uploads.github.com for the same repositories only when ticked", () => {
    const hosts = (uploads: boolean, input: string) =>
      githubRepositoryRules(input, { uploads, graphql: false }).rules.filter(
        (rule) => rule.host === "uploads.github.com",
      );
    expect(hosts(false, "o/r")).toEqual([]);
    expect(hosts(true, "o/r")).toEqual([
      {
        host: "uploads.github.com",
        path_prefix: "/repos/o/r",
        auth: { type: "bearer" },
      },
    ]);
    expect(hosts(true, "o/*")).toEqual([
      {
        host: "uploads.github.com",
        path_prefix: "/repos/o/",
        auth: { type: "bearer" },
      },
    ]);
  });

  it("adds /graphql once, only when ticked", () => {
    const graphql = (tick: boolean) =>
      githubRepositoryRules("a/b\nc/*", {
        uploads: false,
        graphql: tick,
      }).rules.filter((rule) => rule.path_prefix === "/graphql");
    expect(graphql(false)).toEqual([]);
    expect(graphql(true)).toEqual([
      {
        host: "api.github.com",
        path_prefix: "/graphql",
        auth: { type: "bearer" },
      },
    ]);
  });

  it("combines several lines with both ticks", () => {
    const { rules } = githubRepositoryRules("a/b\nc/*", {
      uploads: true,
      graphql: true,
    });
    expect(rules.map((rule) => `${rule.host}${rule.path_prefix}`)).toEqual([
      "api.github.com/repos/a/b",
      "github.com/a/b",
      "github.com/a/b.git",
      "uploads.github.com/repos/a/b",
      "api.github.com/repos/c/",
      "github.com/c/",
      "uploads.github.com/repos/c/",
      "api.github.com/graphql",
    ]);
  });

  it("ignores blank lines and repeats, trims, and drops a .git suffix", () => {
    const { rules, errors } = githubRepositoryRules(
      "\n  o/r  \r\no/r\no/r.git\n\n",
      none,
    );
    expect(errors).toEqual([]);
    expect(rules).toHaveLength(3);
    expect(rules[0].path_prefix).toBe("/repos/o/r");
  });

  it("names each line that is not owner/repo or owner/*", () => {
    expect(
      githubRepositoryRules(
        "https://github.com/o/r\nowner\no/r/extra\no/..\n",
        none,
      ),
    ).toEqual({
      rules: [],
      errors: [
        "https://github.com/o/r is not owner/repo or owner/*",
        "owner is not owner/repo or owner/*",
        "o/r/extra is not owner/repo or owner/*",
        "o/.. is not owner/repo or owner/*",
      ],
    });
    expect(githubRepositoryRules("  \n", none).errors).toEqual([
      "Enter at least one repository, such as owner/repo",
    ]);
  });
});

describe("finding a broader GitHub service", () => {
  const service = (
    changes: Partial<EgressCredentialService>,
  ): EgressCredentialService => ({
    name: "other",
    display_name: "Other",
    description: "",
    is_shared: false,
    can_manage: true,
    configured: false,
    updated_at: null,
    active_source: null,
    key_configured: false,
    key_updated_at: null,
    oauth: null,
    source: "config",
    ...changes,
  });

  it("recognizes the administrator's github service by name or provider", () => {
    expect(
      findBroaderGithubService([
        service({}),
        service({ name: "github", display_name: "GitHub" }),
      ]),
    ).toBe("GitHub");
    expect(
      findBroaderGithubService([
        service({
          name: "code",
          display_name: "Code host",
          oauth: {
            provider: "github",
            display_name: "GitHub",
            connected: false,
            account_label: null,
            can_connect: true,
            reset_required: false,
            service_account: false,
            unavailable_reason: null,
            shared_worker_opt_in: false,
          },
        }),
      ]),
    ).toBe("Code host");
  });

  it("ignores user services, other hosts, and the service being edited", () => {
    expect(
      findBroaderGithubService([service({ name: "github", source: "user" })]),
    ).toBeNull();
    expect(findBroaderGithubService([service({ name: "openai" })])).toBeNull();
    expect(
      findBroaderGithubService([service({ name: "github" })], "github"),
    ).toBeNull();
  });
});

describe("the editor form", () => {
  const renderEditor = (props: Partial<EgressServiceEditorProps> = {}) => {
    const onSave = vi.fn(async () => undefined);
    const onCancel = vi.fn();
    render(
      <EgressServiceEditor
        service={null}
        context="personal"
        onSave={onSave}
        onCancel={onCancel}
        {...props}
      />,
    );
    return { onSave, onCancel };
  };
  const type = (label: string, value: string) =>
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  const save = () =>
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

  it("saves a custom service in its authored form", async () => {
    const { onSave } = renderEditor();
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
    const { onSave } = renderEditor();
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
    const { onSave } = renderEditor();
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
    const { onSave } = renderEditor();
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
    const { onSave } = renderEditor();
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

  it("explains the GraphQL caveat next to the toggle", () => {
    renderEditor();
    expect(
      screen.getByText(
        /a listed GraphQL rule reaches every repository the key can\s+reach/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/fine-grained GitHub token/)).toBeInTheDocument();
  });

  it("shows what is wrong instead of saving an invalid form", async () => {
    const { onSave } = renderEditor();
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
    const { onSave } = renderEditor({ takenNames: ["github"] });
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
    renderEditor({ onSave });
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

  it("cannot rename the service it edits", () => {
    renderEditor({ name: "github", service: { preset: "github" } });
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(screen.getByLabelText("Name")).toHaveValue("github");
    expect(screen.getByLabelText("Preset")).toHaveValue("github");
    expect(
      screen.getByRole("form", { name: "Edit github" }),
    ).toBeInTheDocument();
  });

  it("keeps a preset this page does not know", () => {
    renderEditor({ name: "future", service: { preset: "future_api" } });
    expect(screen.getByLabelText("Preset")).toHaveValue("future_api");
  });

  it("shows the placeholder rules as hints in the advanced section", () => {
    renderEditor();
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

  it("shows the operator's placeholder rules instead on the dashboard", () => {
    renderEditor({ context: "operator" });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(
      screen.getByText(/MINDROOM_ or GIT_CONFIG_ are reserved/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/must be TOKEN, KEY/)).toBeNull();
  });

  it("adds a placeholder with the brokered value and saves it", async () => {
    const { onSave } = renderEditor();
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

  it("opens the advanced section when the service already has placeholders", () => {
    renderEditor({
      name: "svc",
      service: { preset: "github", placeholder_env: { GH_TOKEN: "x" } },
    });
    expect(screen.getByLabelText("Placeholder 1 name")).toHaveValue("GH_TOKEN");
  });

  it("disables the account login on a shared agent", async () => {
    const { onSave } = renderEditor({ sharedTarget: true });
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
    const { onSave } = renderEditor();
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
    const { onSave } = renderEditor({
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
    renderEditor({ name: "github", service: { preset: "github" }, onDelete });
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

  it("has no delete button for a new service", () => {
    renderEditor({ onDelete: vi.fn() });
    expect(screen.queryByRole("button", { name: "Delete service" })).toBeNull();
  });

  it("cancels without saving", () => {
    const { onSave, onCancel } = renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe("the GitHub helper in the editor", () => {
  const renderEditor = (props: Partial<EgressServiceEditorProps> = {}) => {
    const onSave = vi.fn(async () => undefined);
    render(
      <EgressServiceEditor
        service={{ preset: "github" }}
        name="github"
        context="personal"
        onSave={onSave}
        onCancel={vi.fn()}
        {...props}
      />,
    );
    return { onSave };
  };
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
    const { onSave } = renderEditor();
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

  it("makes owner-wide rules and leaves uploads and GraphQL off by default", () => {
    renderEditor();
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

  it("adds uploads and GraphQL rules when ticked", () => {
    renderEditor();
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

  it("names the lines it cannot use and changes nothing", () => {
    renderEditor();
    enter("not a repo");
    apply();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "not a repo is not owner/repo or owner/*",
    );
    expect(screen.queryByLabelText("Rule 1 host")).toBeNull();
  });

  it("is offered for a new custom service and for rules on GitHub hosts only", () => {
    cleanup();
    renderEditor({ name: undefined, service: null });
    expect(screen.getByText("Limit to repositories")).toBeInTheDocument();
    cleanup();
    renderEditor({ name: "openai", service: { preset: "openai" } });
    expect(screen.queryByText("Limit to repositories")).toBeNull();
    cleanup();
    renderEditor({ name: "code", service: { rules: [bearerRule] } });
    expect(screen.queryByText("Limit to repositories")).toBeNull();
    cleanup();
    renderEditor({
      name: "code",
      service: {
        rules: [{ host: "github.com", auth: { type: "bearer" } }],
      },
    });
    expect(screen.getByText("Limit to repositories")).toBeInTheDocument();
  });

  it("warns when the administrator's service already covers every GitHub path", () => {
    renderEditor({ broaderGithubService: "GitHub" });
    const helper = screen.getByRole("region", {
      name: "Limit to repositories",
    });
    expect(within(helper).getByRole("note")).toHaveTextContent(
      "GitHub is configured by your administrator and already covers every GitHub path, so this limit cannot narrow it",
    );
  });

  it("shows no warning without a broader service", () => {
    renderEditor();
    expect(
      within(
        screen.getByRole("region", { name: "Limit to repositories" }),
      ).queryByRole("note"),
    ).toBeNull();
  });
});
