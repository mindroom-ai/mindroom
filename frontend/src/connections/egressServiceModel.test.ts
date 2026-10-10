import { describe, expect, it } from "vitest";
import { EGRESS_PRESETS_FIXTURE } from "@/test/fixtures/egressPresets";
import {
  type EditorOptions,
  type EgressServiceForm,
  type ServiceCoverage,
  findBroaderServices,
  formFromService,
  formRuleSummaries,
  githubRepositoryRules,
  serializeService,
  validateService,
} from "./egressServiceModel";
import type {
  AuthoredEgressService,
  EgressRule,
  EgressRuleSummary,
} from "./types";

type Options = EditorOptions;

const presets = EGRESS_PRESETS_FIXTURE;
const personal: Options = { context: "personal", sharedTarget: false, presets };
const sharedPersonal: Options = {
  context: "personal",
  sharedTarget: true,
  presets,
};
const operator: Options = { context: "operator", sharedTarget: false, presets };
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
  changes: Partial<EgressServiceForm> = {},
  options: Options = personal,
  settings: { editing: boolean; takenNames: string[] } = newService,
) =>
  validateService(
    { ...formFromService("svc", service), ...changes },
    options,
    settings,
  );

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

  it("never writes restrict_to_rules false", () => {
    expect(
      serialize({ rules: [bearerRule], restrict_to_rules: false }),
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

describe("an unchanged save writes the service back as it was", () => {
  it("keeps placeholders a preset service dropped on purpose", () => {
    const service = { preset: "github", placeholder_env: {} };
    expect(serialize(service, operator)).toEqual(service);
    expect(serialize(service, personal)).toEqual(service);
  });

  it("keeps an empty description and an empty or null display name", () => {
    for (const service of [
      { preset: "github", description: "" },
      { preset: "github", display_name: "" },
      { preset: "github", display_name: null },
      { rules: [bearerRule], description: "", placeholder_env: {} },
    ] satisfies AuthoredEgressService[])
      expect(serialize(service, operator)).toEqual(service);
  });

  it("leaves the keys out when the service never had them", () => {
    expect(serialize({ preset: "github" }, operator)).toEqual({
      preset: "github",
    });
  });

  it("writes what the user typed over an empty value", () => {
    const form = formFromService("svc", {
      preset: "github",
      description: "",
      placeholder_env: {},
    });
    expect(
      serializeService(
        {
          ...form,
          description: "Work",
          placeholders: [{ name: "GH_TOKEN", value: "x" }],
        },
        operator,
      ),
    ).toEqual({
      preset: "github",
      description: "Work",
      placeholder_env: { GH_TOKEN: "x" },
    });
  });

  it("drops a value the user cleared instead of writing it as empty", () => {
    const form = formFromService("svc", {
      preset: "github",
      display_name: "Work GitHub",
      description: "Mine",
      placeholder_env: { GH_TOKEN: "x" },
    });
    expect(
      serializeService(
        { ...form, displayName: "", description: "", placeholders: [] },
        operator,
      ),
    ).toEqual({ preset: "github" });
  });

  it.each<[string, AuthoredEgressService]>([
    [
      "bearer with a port and path",
      {
        rules: [
          {
            host: "api.example.com",
            port: 8443,
            path_prefix: "/v1/",
            auth: { type: "bearer" },
          },
        ],
      },
    ],
    [
      "basic with a username",
      {
        rules: [
          {
            host: "git.example.com",
            auth: { type: "basic", username: "x-access-token" },
          },
        ],
      },
    ],
    [
      "header with a template",
      {
        rules: [
          {
            host: "api.example.com",
            auth: {
              type: "header",
              name: "Authorization",
              template: "Bearer {secret}",
            },
          },
        ],
      },
    ],
    [
      "header with the default template left out",
      {
        rules: [
          {
            host: "api.example.com",
            auth: { type: "header", name: "x-api-key" },
          },
        ],
      },
    ],
    [
      "query with a template",
      {
        rules: [
          {
            host: "api.example.com",
            auth: { type: "query", name: "key", template: "k={secret}" },
          },
        ],
      },
    ],
    [
      "one rule of each kind",
      {
        description: "Mixed",
        restrict_to_rules: true,
        rules: [
          { host: "a.example.com", auth: { type: "bearer" } },
          {
            host: "*.b.example.com",
            port: 443,
            auth: { type: "basic", username: "u" },
          },
          {
            host: "c.example.com",
            path_prefix: "/c",
            auth: { type: "query", name: "q" },
          },
        ],
        placeholder_env: { EXAMPLE_API_KEY: "mindroom-brokered" },
      },
    ],
  ])("round-trips %s", (_name, service) => {
    expect(serialize(service, operator)).toEqual(service);
    expect(serialize(service, personal)).toEqual(service);
  });
});

describe("the account login a preset service keeps", () => {
  it("keeps an override written for the same preset", () => {
    expect(
      serialize({ preset: "github", oauth_provider: null }, operator),
    ).toEqual({ preset: "github", oauth_provider: null });
  });

  it("does not carry it over to another preset", () => {
    const form = {
      ...formFromService("svc", { preset: "github", oauth_provider: null }),
      preset: "atlassian",
    };
    expect(
      serializeService(form, operator, {
        preset: "github",
        oauth_provider: null,
      }),
    ).toEqual({ preset: "atlassian" });
  });

  it("does not carry it from a preset onto a custom service", () => {
    const form = {
      ...formFromService("svc", { preset: "github", oauth_provider: null }),
      preset: "",
    };
    expect(
      serializeService(form, operator, {
        preset: "github",
        oauth_provider: null,
      }),
    ).toEqual({ rules: [] });
  });

  it("drops every preset's login on a shared target while the presets are still loading", () => {
    const loading: Options = { ...sharedPersonal, presets: null };
    expect(serialize({ preset: "openai" }, loading)).toEqual({
      preset: "openai",
      oauth_provider: null,
    });
  });
});

describe("rows and fields the model ignores or checks", () => {
  it("skips a placeholder row without a name, whatever its value", () => {
    const form = {
      ...formFromService("svc", { preset: "github" }),
      placeholders: [{ name: "", value: "mindroom-brokered" }],
    };
    expect(validateService(form, personal, newService)).toEqual([]);
    expect(serializeService(form, personal)).toEqual({ preset: "github" });
  });

  it("requires an account login to look like a provider id", () => {
    for (const oauthProvider of ["GitHub", "git hub", "-x", "a/b"])
      expect(errorsFor(null, { rules: [validRule()], oauthProvider })).toEqual([
        "Account login must be a provider id such as github or google_drive (lowercase letters, digits, - and _)",
      ]);
    expect(
      errorsFor(null, { rules: [validRule()], oauthProvider: "google_drive" }),
    ).toEqual([]);
    // A shared agent sends no login, so a stale value cannot block the save.
    expect(
      errorsFor(
        null,
        { rules: [validRule()], oauthProvider: "GitHub" },
        sharedPersonal,
      ),
    ).toEqual([]);
  });

  it.each([
    "Host",
    "content-length",
    "Transfer-Encoding",
    "proxy-authorization",
  ])("refuses the broker's header %s", (name) => {
    expect(
      errorsFor(null, {
        rules: [{ ...validRule(), authType: "header", headerOrParam: name }],
      }),
    ).toEqual([`Rule 1: header name ${name} is reserved for the broker`]);
  });

  it("caps GitHub owner and repository names", () => {
    const owner = "o".repeat(40);
    const repo = "r".repeat(101);
    const none = { uploads: false, graphql: false };
    expect(githubRepositoryRules(`${owner}/r`, none).errors).toEqual([
      `${owner}/r is too long: GitHub owners have at most 39 characters and repositories 100`,
    ]);
    expect(githubRepositoryRules(`o/${repo}`, none).errors).toHaveLength(1);
    expect(
      githubRepositoryRules(`${"o".repeat(39)}/${"r".repeat(100)}`, none)
        .errors,
    ).toEqual([]);
    expect(githubRepositoryRules(`${owner}/*`, none).errors).toHaveLength(1);
  });
});

describe("finding services that already cover a rule", () => {
  const rule = (
    host: string,
    path_prefix: string,
    port: number | null = null,
  ): EgressRuleSummary => ({ host, port, path_prefix });
  const service = (display_name: string, ...rules: EgressRuleSummary[]) => ({
    name: display_name.toLowerCase(),
    display_name,
    rules,
  });
  const covered = (mine: EgressRuleSummary[], ...others: ServiceCoverage[]) =>
    findBroaderServices(mine, others);

  it("names a service with a shorter prefix on the same host", () => {
    expect(
      covered(
        [rule("api.github.com", "/repos/o/r")],
        service("GitHub", rule("api.github.com", "/")),
      ),
    ).toEqual(["GitHub"]);
  });

  it("covers a trailing-slash prefix by plain prefix and others by whole segment", () => {
    const mine = [rule("api.github.com", "/repos/o/r")];
    expect(
      covered(mine, service("Owner", rule("api.github.com", "/repos/o/"))),
    ).toEqual(["Owner"]);
    expect(
      covered(mine, service("Owner", rule("api.github.com", "/repos/o"))),
    ).toEqual(["Owner"]);
    // Not the same segment: `/repos/o` never matches `/repos/other`.
    expect(
      covered(
        [rule("api.github.com", "/repos/other/r")],
        service("Owner", rule("api.github.com", "/repos/o")),
      ),
    ).toEqual([]);
    expect(
      covered(
        [rule("api.github.com", "/repos/other/r")],
        service("Owner", rule("api.github.com", "/repos/o/")),
      ),
    ).toEqual([]);
  });

  it("ignores equal or longer prefixes, other hosts, and other ports", () => {
    const mine = [rule("api.github.com", "/repos/o/r")];
    expect(
      covered(mine, service("Same", rule("api.github.com", "/repos/o/r"))),
    ).toEqual([]);
    expect(
      covered(
        mine,
        service("Longer", rule("api.github.com", "/repos/o/r/issues")),
      ),
    ).toEqual([]);
    expect(covered(mine, service("Other", rule("github.com", "/")))).toEqual(
      [],
    );
    expect(
      covered(mine, service("Port", rule("api.github.com", "/", 8443))),
    ).toEqual([]);
    expect(covered(mine, service("None"))).toEqual([]);
  });

  it("matches the port, or any port, and ignores host case", () => {
    expect(
      covered(
        [rule("Api.Example.com", "/v1/x", 8443)],
        service("Any", rule("api.example.com", "/")),
        service("Same", rule("api.example.com", "/v1/", 8443)),
        service("Different", rule("api.example.com", "/", 9000)),
      ),
    ).toEqual(["Any", "Same"]);
    // A rule for any port is not covered by one that names a port.
    expect(
      covered(
        [rule("api.example.com", "/v1/x")],
        service("Port", rule("api.example.com", "/", 8443)),
      ),
    ).toEqual([]);
  });

  it("names each service once, in listing order, and tolerates missing rules", () => {
    expect(
      covered(
        [rule("a.example.com", "/x/y"), rule("b.example.com", "/x/y")],
        service("Both", rule("a.example.com", "/"), rule("b.example.com", "/")),
        service("First", rule("a.example.com", "/x")),
        { name: "old", display_name: "Old" },
      ),
    ).toEqual(["Both", "First"]);
  });

  it("reads the form's own rules, or the preset's when it keeps them", () => {
    const own = formFromService("svc", {
      rules: [
        { host: " API.Example.com ", port: 8443, auth: { type: "bearer" } },
        { host: "b.example.com", path_prefix: "/v1", auth: { type: "bearer" } },
      ],
    });
    expect(formRuleSummaries(own, presets)).toEqual([
      rule("api.example.com", "/", 8443),
      rule("b.example.com", "/v1"),
    ]);
    const blank = { ...own, rules: [{ ...own.rules[0], host: "  " }] };
    expect(formRuleSummaries(blank, presets)).toEqual([]);
    const preset = formFromService("svc", { preset: "google_drive" });
    expect(formRuleSummaries(preset, presets)).toEqual([
      rule("www.googleapis.com", "/drive/"),
      rule("www.googleapis.com", "/upload/drive/"),
    ]);
    expect(formRuleSummaries(preset, null)).toEqual([]);
    const replaced = formFromService("svc", {
      preset: "google_drive",
      rules: [{ host: "x.example.com", auth: { type: "bearer" } }],
    });
    expect(formRuleSummaries(replaced, presets)).toEqual([
      rule("x.example.com", "/"),
    ]);
  });
});
