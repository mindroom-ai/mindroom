import { describe, expect, it } from "vitest";
import {
  applyAuthChoice,
  emptyRule,
  formFromService,
  ruleFormFrom,
  serializeService,
  splitHostInput,
  suggestedName,
  usesMoreOptions,
  type EditorOptions,
  type EgressServiceForm,
} from "./egressServiceForm";
import {
  personal,
  sharedPersonal,
  operator,
  newService,
  bearerRule,
  serialize,
  problemsFor,
  errorsFor,
  basicAuth,
  validRule,
} from "@/test/egressServiceTestKit";
import { validateService } from "./egressServiceValidation";
import { githubRepositoryRules } from "./githubRepositoryRules";
import type { AuthoredEgressService, EgressRule } from "./types";

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
    ).toEqual({
      preset: "github",
      display_name: "GitHub work",
    });
    expect(serialize({ preset: "github", rules: [bearerRule] })).toEqual({
      preset: "github",
      rules: [bearerRule],
    });
  });

  it("never writes restrict_to_rules false", () => {
    expect(
      serialize({ rules: [bearerRule], restrict_to_rules: false }),
    ).toEqual({
      rules: [bearerRule],
    });
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
    ).toEqual({
      rules: [bearerRule],
    });
  });

  it("keeps an operator's oauth_on_shared_workers and never sends it from the personal page", () => {
    const service = {
      preset: "github",
      oauth_on_shared_workers: true,
    } satisfies AuthoredEgressService;
    expect(serialize(service, operator)).toEqual(service);
    expect(serialize(service, personal)).toEqual({ preset: "github" });
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

  it("opens a rule with a null port as any port and saves it without an error", () => {
    const service: AuthoredEgressService = {
      rules: [{ ...bearerRule, port: null }],
    };
    expect(formFromService("svc", service).rules[0].port).toBe("");
    expect(
      problemsFor(service, {}, personal, { editing: true, takenNames: [] }),
    ).toEqual([]);
    expect(serialize(service)).toEqual({ rules: [bearerRule] });
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
    ).toEqual({
      preset: "github",
      oauth_provider: null,
    });
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
});

describe("the way the key is sent", () => {
  const header = (template?: string): AuthoredEgressService => ({
    rules: [
      {
        host: "api.example.com",
        auth: { type: "header", name: "Authorization", template },
      },
    ],
  });

  it("shows a header with a custom template as a custom template", () => {
    expect(
      formFromService("svc", header("Token {secret}")).rules[0],
    ).toMatchObject({
      authType: "template",
      headerOrParam: "Authorization",
    });
    expect(formFromService("svc", header()).rules[0].authType).toBe("header");
    expect(formFromService("svc", header("{secret}")).rules[0].authType).toBe(
      "header",
    );
  });

  it("writes a custom template as header auth with its template", () => {
    const service = header("Token {secret}");
    expect(serialize(service)).toEqual(service);
  });

  it("drops a custom template when the user chooses Header", () => {
    const custom = formFromService("svc", header("Token {secret}")).rules[0];
    expect(applyAuthChoice(custom, "header")).toMatchObject({
      authType: "header",
      template: "",
    });
    expect(applyAuthChoice(custom, "bearer").authType).toBe("bearer");
    expect(applyAuthChoice(emptyRule(), "template").authType).toBe("template");
  });

  it("asks a custom template for its template, with the key once", () => {
    const custom = (template: string) => ({
      rules: [
        {
          ...validRule(),
          authType: "template" as const,
          headerOrParam: "Authorization",
          template,
        },
      ],
    });
    expect(errorsFor(null, custom(""))).toEqual([
      "Template must contain {secret} exactly once",
    ]);
    expect(errorsFor(null, custom("Token"))).toEqual([
      "Template must contain {secret} exactly once",
    ]);
    expect(errorsFor(null, custom("Token {secret}"))).toEqual([]);
    expect(
      problemsFor(null, custom("")).map((problem) => problem.field),
    ).toEqual(["rule:0:template"]);
  });

  it("needs a header name for a custom template too", () => {
    expect(
      errorsFor(null, {
        rules: [
          { ...validRule(), authType: "template", template: "Token {secret}" },
        ],
      }),
    ).toEqual(["Header auth needs a header name"]);
  });
});

describe("suggesting a name", () => {
  const form = (preset: string, host = "") => ({
    ...formFromService("", null),
    preset,
    rules: [{ ...validRule(), host }],
  });

  it("uses the preset id, then the host's first label", () => {
    expect(suggestedName(form("github", ""))).toBe("github");
    expect(suggestedName(form("google_drive", "ignored.example.com"))).toBe(
      "google_drive",
    );
    expect(suggestedName(form("", "httpbin.org"))).toBe("httpbin");
    expect(suggestedName(form("", " API.Example.com "))).toBe("api");
  });

  it("skips a wildcard and drops what a name cannot hold", () => {
    expect(suggestedName(form("", "*.example.com"))).toBe("example");
    expect(suggestedName(form("", "my_host-1.example.com"))).toBe("my_host-1");
    expect(suggestedName(form("", "localhost:8080"))).toBe("localhost");
    expect(suggestedName(form("", "-odd.example.com"))).toBe("odd");
    expect(suggestedName(form("", "*"))).toBe("");
  });

  it("suggests nothing without a preset or a host", () => {
    expect(suggestedName(form("", ""))).toBe("");
    expect(suggestedName({ ...form("", ""), rules: [] })).toBe("");
  });

  it("takes the first of base, base-2, and base-3 that is free", () => {
    expect(suggestedName(form("github"), [])).toBe("github");
    expect(suggestedName(form("github"), ["openai"])).toBe("github");
    expect(suggestedName(form("github"), ["github"])).toBe("github-2");
    expect(suggestedName(form("github"), ["github", "github-2"])).toBe(
      "github-3",
    );
    // A later gap does not matter: the order is base, -2, -3.
    expect(
      suggestedName(form("", "httpbin.org"), ["httpbin", "httpbin-3"]),
    ).toBe("httpbin-2");
  });

  it("falls back to the base, so the clash is named, when all three are taken", () => {
    expect(
      suggestedName(form("github"), ["github", "github-2", "github-3"]),
    ).toBe("github");
  });

  it("keeps the suffix inside the 63 characters a name may have", () => {
    const base = "a".repeat(63);
    const name = suggestedName(form("", `${base}.example.com`), [base]);
    expect(name).toBe(`${"a".repeat(61)}-2`);
    expect(name).toHaveLength(63);
  });

  it("suggests nothing, and no suffix, without a base", () => {
    expect(suggestedName(form("", ""), [""])).toBe("");
  });
});

describe("knowing when the form uses more than the basic fields", () => {
  const blank = formFromService("svc", null);

  it("is false for a new service and for a bare preset", () => {
    expect(usesMoreOptions(blank)).toBe(false);
    expect(usesMoreOptions(formFromService("svc", { preset: "github" }))).toBe(
      false,
    );
  });

  it.each<[string, Partial<EgressServiceForm>]>([
    ["a display name", { displayName: "Mine" }],
    ["a description", { description: "Mine" }],
    ["a path prefix", { rules: [{ ...validRule(), pathPrefix: "/v1" }] }],
    ["a port", { rules: [{ ...validRule(), port: "8443" }] }],
    ["a second rule", { rules: [validRule(), validRule()] }],
    ["replaced preset rules", { preset: "openai", replacePresetRules: true }],
    ["the restriction", { restrictToRules: true }],
    ["an account login", { oauthProvider: "github" }],
    ["a placeholder", { placeholders: [{ name: "A_KEY", value: "x" }] }],
  ])("is true with %s", (_name, changes) => {
    expect(usesMoreOptions({ ...blank, ...changes })).toBe(true);
  });
});

describe("the display name a new custom service gets", () => {
  const add = (
    service: AuthoredEgressService,
    options: EditorOptions = personal,
  ) => serializeService(formFromService("svc", service), options);

  it("is the first rule's host, lowercased, and not a title-cased name", () => {
    expect(
      add({ rules: [{ ...bearerRule, host: "Echo-Test.example.com" }] }),
    ).toMatchObject({ display_name: "echo-test.example.com" });
  });

  it("gives way to a display name that is typed", () => {
    expect(
      add({ display_name: "Echo", rules: [bearerRule] }).display_name,
    ).toBe("Echo");
  });

  it("is left to the server for a preset, and for a service that is edited", () => {
    expect(add({ preset: "openai" })).toEqual({ preset: "openai" });
    expect(serialize({ rules: [bearerRule] })).toEqual({ rules: [bearerRule] });
  });

  it("is left alone for a display name emptied on purpose", () => {
    expect(
      add({ display_name: null, rules: [bearerRule] }).display_name,
    ).toBeNull();
    expect(add({ display_name: "", rules: [bearerRule] }).display_name).toBe(
      "",
    );
  });

  it("is written on the dashboard too", () => {
    expect(add({ rules: [bearerRule] }, operator).display_name).toBe(
      "api.example.com",
    );
  });
});

describe("what the Website field keeps", () => {
  it.each([
    ["httpbin.org", "httpbin.org", ""],
    ["https://httpbin.org", "httpbin.org", ""],
    ["http://httpbin.org/anything", "httpbin.org", "/anything"],
    ["https://httpbin.org/a?b=1#c", "httpbin.org", "/a"],
    ["httpbin.org/anything", "httpbin.org", "/anything"],
    ["httpbin.org/v1/", "httpbin.org", "/v1/"],
    ["httpbin.org/", "httpbin.org", "/"],
    ["httpbin.org?x=1", "httpbin.org", ""],
    ["httpbin.org#top", "httpbin.org", ""],
    ["https://api.example.com:8443/v1", "api.example.com:8443", "/v1"],
    ["*.example.com/x", "*.example.com", "/x"],
    ["https://", "", ""],
    ["https:/", "https:", "/"],
    ["", "", ""],
  ])("turns %j into the host %j and the path %j", (typed, host, path) => {
    expect(splitHostInput(typed)).toEqual({ host, path });
  });
});

describe("a service that only limits paths", () => {
  // Its rules send nothing, so the server keeps them without auth.
  const noAuth = (rules: EgressRule[]): EgressRule[] =>
    rules.map(({ auth: _auth, ...rule }) => rule);
  const limit: AuthoredEgressService = {
    display_name: "GitHub (a/b)",
    description: "Limited to a/b",
    rules: noAuth(
      githubRepositoryRules("a/b", { uploads: false, graphql: false }).rules,
    ),
    restrict_to_rules: true,
    restrict_only: true,
  };

  it("round-trips through the form, with restrict_only and the restriction on", () => {
    const form = formFromService("github-limited", limit);
    expect(form.restrictOnly).toBe(true);
    expect(form.restrictToRules).toBe(true);
    expect(serializeService(form, personal, limit)).toEqual(limit);
  });

  it("opens a limit whose rules have no auth, with the default way of sending in the form", () => {
    const form = formFromService("github-limited", limit);
    expect(form.rules.map((rule) => rule.authType)).toEqual([
      "bearer",
      "bearer",
      "bearer",
    ]);
    expect(form.rules[0]).toMatchObject({
      host: "api.github.com",
      username: "",
      headerOrParam: "",
      template: "",
    });
  });

  it("writes the rules of a limit without auth, even when the form's rules came from the helper with it", () => {
    const form = {
      ...formFromService("github-limited", null),
      restrictOnly: true,
      rules: githubRepositoryRules("a/b", {
        uploads: false,
        graphql: false,
      }).rules.map(ruleFormFrom),
    };
    const written = serializeService(form, personal);
    expect(written.rules).toEqual(limit.rules);
    expect(written.rules?.some((rule) => "auth" in rule)).toBe(false);
  });

  it("writes no login, placeholders, or preset, whatever the form holds", () => {
    const form = {
      ...formFromService("github-limited", limit),
      oauthProvider: "github",
      placeholders: [{ name: "GH_TOKEN", value: "mindroom-brokered" }],
      preset: "github",
    };
    expect(serializeService(form, personal, limit)).toEqual(limit);
    const written = serializeService(form, personal, limit);
    expect(written).not.toHaveProperty("oauth_provider");
    expect(written).not.toHaveProperty("placeholder_env");
  });

  it("starts a new one with no rules, and needs repositories to be saved", () => {
    const form = {
      ...formFromService("github-limited", null),
      restrictOnly: true,
      rules: [],
    };
    // The helper shows this one beside the repositories.
    expect(validateService(form, personal, newService)).toEqual([
      {
        field: "rules",
        message: "Enter at least one repository, such as owner/repo",
      },
    ]);
  });

  it("checks only the name otherwise, since the helper wrote the rules", () => {
    const form = { ...formFromService("Bad Name", limit) };
    expect(
      validateService(form, personal, newService).map((p) => p.field),
    ).toEqual(["name"]);
    expect(
      validateService(formFromService("github-limited", limit), personal, {
        editing: true,
        takenNames: ["github-limited"],
        service: limit,
      }),
    ).toEqual([]);
  });

  it("refuses more than 50 rules, which a long list of repositories can make", () => {
    const repositories = Array.from({ length: 17 }, (_, i) => `o/r${i}`);
    const rules = noAuth(
      githubRepositoryRules(repositories.join("\n"), {
        uploads: false,
        graphql: false,
      }).rules,
    );
    expect(rules).toHaveLength(51);
    const form = (count: number) => ({
      ...formFromService("github-limited", { ...limit, rules }),
      rules: rules.slice(0, count).map(ruleFormFrom),
    });
    expect(validateService(form(51), personal, newService)).toEqual([
      { field: "form", message: "A service can have at most 50 rules" },
    ]);
    expect(validateService(form(50), personal, newService)).toEqual([]);
    // The dashboard has no such limit.
    expect(validateService(form(51), operator, newService)).toEqual([]);
  });

  it("refuses a limit larger than 16 KiB, as any personal service is", () => {
    const form = {
      ...formFromService("github-limited", limit),
      displayName: "x".repeat(17 * 1024),
    };
    expect(validateService(form, personal, newService)).toEqual([
      { field: "form", message: "A service can take at most 16 KiB" },
    ]);
    expect(validateService(form, operator, newService)).toEqual([]);
  });

  it("does not make the other forms restrict-only", () => {
    expect(formFromService("svc", { rules: [bearerRule] }).restrictOnly).toBe(
      false,
    );
    expect(
      serializeService(
        formFromService("svc", { rules: [bearerRule] }),
        personal,
      ),
    ).not.toHaveProperty("restrict_only");
  });
});
