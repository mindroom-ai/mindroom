import { describe, expect, it } from "vitest";
import {
  formFromService,
  serializeService,
  type EgressServiceForm,
} from "./egressServiceForm";
import {
  personal,
  sharedPersonal,
  operator,
  newService,
  problemsFor,
  errorsFor,
  validation,
  validRule,
} from "@/test/egressServiceTestKit";
import {
  BASIC_RULE_PARTS,
  isBasicField,
  placeholderField,
  ruleField,
  validateService,
} from "./egressServiceValidation";
import { githubRepositoryRules } from "./githubRepositoryRules";
import type {
  AuthoredEgressService,
  EgressValidation,
  EgressPlaceholderValidation,
} from "./types";

const placeholderErrors = (
  name: string,
  options = personal,
  value = "mindroom-brokered",
) =>
  errorsFor({ preset: "github" }, { placeholders: [{ name, value }] }, options);

describe("validating a service", () => {
  it("accepts a preset alone and a valid custom service", () => {
    expect(errorsFor({ preset: "github" })).toEqual([]);
    expect(errorsFor(null, { name: "svc", rules: [validRule()] })).toEqual([]);
  });

  it("checks the name of a new service only", () => {
    expect(errorsFor({ preset: "github" }, { name: "" })).toEqual([
      "Name is required",
    ]);
    expect(errorsFor({ preset: "github" }, { name: "  " })[0]).toContain(
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
    ["", "Host is required"],
    ["api.example.com:8443", "Host must not contain a port"],
    ["*.*.example.com", "Host can only have one wildcard label"],
    ["*.com", "Wildcard host must have at least one domain part"],
    ["api.*.com", "Wildcard must be a single leading *. label"],
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
    ).toEqual(["Path prefix must start with /"]);
    for (const port of ["0", "70000", "abc", "1.5"])
      expect(errorsFor(null, { rules: [{ ...validRule(), port }] })).toEqual([
        "Port must be a number from 1 to 65535",
      ]);
    expect(
      errorsFor(null, { rules: [{ ...validRule(), port: "8443" }] }),
    ).toEqual([]);
  });

  it("checks the fields each auth type needs", () => {
    const check = (changes: object) =>
      errorsFor(null, { rules: [{ ...validRule(), ...changes }] });
    expect(check({ authType: "basic", username: " " })).toEqual([
      "Basic auth needs a username",
    ]);
    expect(check({ authType: "header" })).toEqual([
      "Header auth needs a header name",
    ]);
    expect(check({ authType: "header", headerOrParam: "bad header" })).toEqual([
      "Header name must be a valid HTTP header name",
    ]);
    expect(check({ authType: "query" })).toEqual([
      "Query auth needs a parameter name",
    ]);
    expect(
      check({ authType: "header", headerOrParam: "x-key", template: "token" }),
    ).toEqual(["Template must contain {secret} exactly once"]);
    expect(
      check({
        authType: "query",
        headerOrParam: "key",
        template: "{secret}{secret}",
      }),
    ).toEqual(["Template must contain {secret} exactly once"]);
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
    ).toEqual(["Host is required"]);
  });

  it("caps a personal service at 50 rules but not an operator service", () => {
    const rules = Array.from({ length: 51 }, validRule);
    expect(errorsFor(null, { rules })).toEqual([
      "A service can have at most 50 rules",
    ]);
    expect(errorsFor(null, { rules }, operator)).toEqual([]);
  });

  it("caps a personal service at 16 KiB", () => {
    const description = "x".repeat(17 * 1024);
    expect(errorsFor(null, { rules: [validRule()], description })).toEqual([
      "A service can take at most 16 KiB",
    ]);
    expect(
      errorsFor(null, { rules: [validRule()], description }, operator),
    ).toEqual([]);
  });

  it("applies the personal placeholder rules to personal services only", () => {
    expect(placeholderErrors("MY_API_KEY")).toEqual([]);
    expect(placeholderErrors("LD_PRELOAD")[0]).toBe(
      "LD_PRELOAD is not allowed as a placeholder: name a credential, such as MY_API_KEY, with TOKEN, KEY, SECRET, PASSWORD, PAT, AUTH, or CREDENTIAL as one of its words.",
    );
    // A credential word has to be a whole part: PYTHONPATH only contains PAT.
    expect(placeholderErrors("PYTHONPATH")).toHaveLength(1);
    expect(placeholderErrors("MY_PAT")).toEqual([]);
    expect(placeholderErrors("MY_API_KEY", personal, "has space")[0]).toBe(
      "The value of MY_API_KEY must be 1 to 256 letters, digits, or . _ : -",
    );
    expect(placeholderErrors("MY_API_KEY", personal, "")).toHaveLength(1);
    // The operator may use any valid variable name and any value.
    expect(placeholderErrors("LD_PRELOAD", operator, "a b")).toEqual([]);
  });

  describe("placeholder names the server refuses", () => {
    it.each([
      "PATH",
      "HOME",
      "HTTP_PROXY",
      "HTTPS_PROXY",
      "NO_PROXY",
      "ALL_PROXY",
      "NODE_USE_ENV_PROXY",
      "SSL_CERT_FILE",
      "REQUESTS_CA_BUNDLE",
      "CURL_CA_BUNDLE",
      "GIT_SSL_CAINFO",
      "NODE_EXTRA_CA_CERTS",
    ])("reserves %s for the broker in every context", (name) => {
      expect(placeholderErrors(name, operator)).toEqual([
        `Placeholder name ${name} is reserved`,
      ]);
      // One reason is enough: a personal service is not also told about its words.
      expect(placeholderErrors(name)).toEqual([
        `Placeholder name ${name} is reserved`,
      ]);
    });

    it.each([
      ["MINDROOM_API_KEY", "MINDROOM_"],
      ["GIT_CONFIG_KEY_0", "GIT_CONFIG_"],
    ])("reserves the prefix of %s in every context", (name, prefix) => {
      const message = `Placeholder name ${name} starts with reserved prefix ${prefix}`;
      expect(placeholderErrors(name, operator)).toEqual([message]);
      expect(placeholderErrors(name)).toEqual([message]);
    });

    it.each([
      "SSH_AUTH_SOCK",
      "SSH_TOKEN",
      "RESTIC_PASSWORD_COMMAND",
      "SOPS_AGE_KEY_CMD",
      "MY_KEY_FILE",
      "TOOL_TOKEN_DIR",
      "GIT_CREDENTIAL_HELPER",
      "MY_API_KEY_PROVIDER",
      "MY_KEY_OPTS",
      "MY_KEY_OPTIONS",
      "MY_KEY_EXTENSIONS",
    ])("refuses %s on a personal service only", (name) => {
      const errors = placeholderErrors(name);
      expect(errors).toHaveLength(1);
      expect(errors[0]).toBe(
        `${name} is not allowed: tools read names with COMMAND, CMD, FILE, DIR, PROVIDER, HELPER, OPTS, OPTIONS, or EXTENSIONS, or starting with SSH_, as programs or paths.`,
      );
      expect(placeholderErrors(name, operator)).toEqual([]);
    });

    it("refuses whole words only, and SSH_ only at the start", () => {
      for (const name of [
        "MY_FILES_KEY",
        "COMMANDER_TOKEN",
        "MY_SSH_KEY",
        "SSHKEY_TOKEN",
        "MY_DIRECTORY_KEY",
        "OPTIONAL_TOKEN",
      ])
        expect(placeholderErrors(name)).toEqual([]);
    });

    it("keeps the credential word rule ahead of the refused words", () => {
      // LD_PRELOAD has no credential word, so that is what the user hears.
      expect(placeholderErrors("LD_PRELOAD")[0]).toContain("name a credential");
      expect(placeholderErrors("SSH_AGENT_PID")[0]).toContain(
        "name a credential",
      );
    });
  });

  it("measures a personal service the way the server does, in ASCII escapes", () => {
    // Python's json.dumps writes each character outside printable ASCII as a
    // 6 byte \uXXXX escape, and an emoji as two of them.
    const serverSize = (service: AuthoredEgressService) =>
      JSON.stringify(service).replace(
        /[^\x20-\x7e]/g,
        (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
      ).length;
    const limit = 16 * 1024;
    const form = (description: string) => ({
      ...formFromService("svc", null),
      rules: [validRule()],
      description,
    });
    const sizeWith = (description: string) =>
      serverSize(serializeService(form(description), personal));
    const fits = (description: string) =>
      validateService(form(description), personal, newService).map(
        (problem) => problem.message,
      );
    // How many of `char`, each costing `cost` bytes, make the service exactly 16 KiB or just over.
    const largest = (char: string, cost: number) =>
      Math.floor((limit - (sizeWith(char) - cost)) / cost);

    // The sizes the server's `_json_size` reports for the same service, which
    // a new service writes with its website as the display name.
    expect(sizeWith("a")).toBe(114);
    expect(sizeWith("\u00e9")).toBe(119);
    expect(sizeWith("\u{1F600}")).toBe(125);
    expect(sizeWith("\u007f")).toBe(119);

    const plain = largest("a", 1);
    expect(sizeWith("a".repeat(plain))).toBe(limit);
    expect(fits("a".repeat(plain))).toEqual([]);
    expect(fits("a".repeat(plain + 1))).toEqual([
      "A service can take at most 16 KiB",
    ]);

    // 2 bytes in UTF-8 but 6 in the server's count: UTF-8 would let this through.
    const accented = largest("\u00e9", 6);
    expect(fits("\u00e9".repeat(accented))).toEqual([]);
    expect(fits("\u00e9".repeat(accented + 1))).toEqual([
      "A service can take at most 16 KiB",
    ]);
    expect(
      new TextEncoder().encode(
        JSON.stringify(
          serializeService(form("\u00e9".repeat(accented + 1)), personal),
        ),
      ).length,
    ).toBeLessThan(limit);

    // An emoji is a surrogate pair: 12 bytes. DEL is not escaped by JSON.stringify but is by the server: 6.
    const emoji = largest("\u{1F600}", 12);
    expect(fits("\u{1F600}".repeat(emoji))).toEqual([]);
    expect(fits("\u{1F600}".repeat(emoji + 1))).toHaveLength(1);
    const del = largest("\u007f", 6);
    expect(fits("\u007f".repeat(del))).toEqual([]);
    expect(fits("\u007f".repeat(del + 1))).toHaveLength(1);
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
    ).toEqual([`Header name ${name} is reserved for the broker`]);
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

describe("where each problem is shown", () => {
  const rule = (changes: object) => ({ ...validRule(), ...changes });
  const fields = (changes: Partial<EgressServiceForm>) =>
    problemsFor(null, changes).map((problem) => problem.field);

  it("builds one id per rule field and placeholder field", () => {
    expect(ruleField(2, "host")).toBe("rule:2:host");
    expect(placeholderField(0, "value")).toBe("placeholder:0:value");
  });

  it("ties a problem to the field that shows it", () => {
    expect(problemsFor({ preset: "github" }, { name: "" })).toEqual([
      { field: "name", message: "Name is required" },
    ]);
    expect(fields({ rules: [rule({ host: "" })] })).toEqual(["rule:0:host"]);
    expect(fields({ rules: [validRule(), rule({ host: "" })] })).toEqual([
      "rule:1:host",
    ]);
    expect(fields({ rules: [rule({ pathPrefix: "v1" })] })).toEqual([
      "rule:0:pathPrefix",
    ]);
    expect(fields({ rules: [rule({ port: "0" })] })).toEqual(["rule:0:port"]);
    expect(fields({ rules: [rule({ authType: "basic" })] })).toEqual([
      "rule:0:username",
    ]);
    expect(fields({ rules: [rule({ authType: "header" })] })).toEqual([
      "rule:0:authName",
    ]);
    expect(fields({ rules: [rule({ authType: "query" })] })).toEqual([
      "rule:0:authName",
    ]);
    expect(
      fields({
        rules: [
          rule({ authType: "header", headerOrParam: "x", template: "none" }),
        ],
      }),
    ).toEqual(["rule:0:template"]);
    expect(fields({ rules: [] })).toEqual(["rules"]);
    expect(fields({ rules: [validRule()], oauthProvider: "Git Hub" })).toEqual([
      "oauthProvider",
    ]);
  });

  it("ties a placeholder problem to its name or its value", () => {
    const rows = (...placeholders: [string, string][]) => ({
      rules: [validRule()],
      placeholders: placeholders.map(([name, value]) => ({ name, value })),
    });
    expect(fields(rows(["LD_PRELOAD", "x"]))).toEqual(["placeholder:0:name"]);
    expect(fields(rows(["MY_API_KEY", "has space"]))).toEqual([
      "placeholder:0:value",
    ]);
    expect(
      fields(rows(["MY_API_KEY", "a"], ["MY_API_KEY", "b"], ["x", "y"])),
    ).toEqual(["placeholder:1:name", "placeholder:2:name"]);
  });

  it("keeps the size problem for the form as a whole", () => {
    expect(
      problemsFor(null, {
        rules: [validRule()],
        description: "x".repeat(17 * 1024),
      }),
    ).toEqual([
      { field: "form", message: "A service can take at most 16 KiB" },
    ]);
  });

  it("gives a lowercase personal placeholder the server's wording, not a pattern", () => {
    expect(
      errorsFor(
        { preset: "github" },
        {
          placeholders: [{ name: "my_key", value: "x" }],
        },
      ),
    ).toEqual([
      "my_key is not allowed as a placeholder: name a credential, such as MY_API_KEY, with TOKEN, KEY, SECRET, PASSWORD, PAT, AUTH, or CREDENTIAL as one of its words.",
    ]);
  });
});

describe("which problems the basic fields show", () => {
  it("lists the name and the first rule's host and way of sending", () => {
    expect(BASIC_RULE_PARTS).toEqual([
      "host",
      "username",
      "authName",
      "template",
    ]);
    for (const field of [
      "name",
      ...BASIC_RULE_PARTS.map((part) => ruleField(0, part)),
    ])
      expect(isBasicField(field)).toBe(true);
  });

  it("leaves everything else to More options", () => {
    for (const field of [
      ruleField(0, "pathPrefix"),
      ruleField(0, "port"),
      ruleField(1, "host"),
      ruleField(1, "authName"),
      "rules",
      "oauthProvider",
      "form",
      placeholderField(0, "name"),
    ])
      expect(isBasicField(field)).toBe(false);
  });
});

describe("the validation the server sends", () => {
  const optionsWith = (
    changes: Partial<EgressValidation> = {},
    placeholder: Partial<EgressPlaceholderValidation> = {},
    base = personal,
  ): typeof personal => ({
    ...base,
    validation: {
      ...validation,
      ...changes,
      placeholder: { ...validation.placeholder, ...placeholder },
    },
  });
  const withoutValidation = { ...personal, validation: null };

  it("has name and value patterns from Python that read as JavaScript regular expressions", () => {
    for (const source of [
      validation.placeholder.name_pattern,
      validation.placeholder.value_pattern,
    ])
      expect(() => new RegExp(`^(?:${source})$`)).not.toThrow();
  });

  it("takes the most rules from the server", () => {
    const rules = Array.from({ length: 3 }, validRule);
    const two = optionsWith({ max_rules_per_service: 2 });
    expect(errorsFor(null, { rules }, two)).toEqual([
      "A service can have at most 2 rules",
    ]);
    expect(errorsFor(null, { rules: rules.slice(0, 2) }, two)).toEqual([]);
    expect(
      errorsFor(
        null,
        { rules },
        optionsWith({ max_rules_per_service: 2 }, {}, operator),
      ),
    ).toEqual([]);
  });

  it("takes the most bytes from the server", () => {
    const description = "x".repeat(3 * 1024);
    expect(
      errorsFor(
        null,
        { rules: [validRule()], description },
        optionsWith({ max_service_bytes: 2048 }),
      ),
    ).toEqual(["A service can take at most 2 KiB"]);
    expect(
      errorsFor(
        null,
        { rules: [validRule()], description },
        optionsWith({ max_service_bytes: 4096 }),
      ),
    ).toEqual([]);
  });

  it("takes the credential and refused words from the server", () => {
    const options = optionsWith(
      {},
      {
        credential_words: ["WIDGET", "GADGET"],
        refused_words: ["BLOB", "CLOB"],
        refused_prefix: "ZZ_",
      },
    );
    expect(placeholderErrors("MY_WIDGET", options)).toEqual([]);
    expect(placeholderErrors("MY_API_KEY", options)).toEqual([
      "MY_API_KEY is not allowed as a placeholder: name a credential, such as MY_API_KEY, with WIDGET, or GADGET as one of its words.",
    ]);
    expect(placeholderErrors("MY_GADGET_BLOB", options)).toEqual([
      "MY_GADGET_BLOB is not allowed: tools read names with BLOB, or CLOB, or starting with ZZ_, as programs or paths.",
    ]);
    expect(placeholderErrors("ZZ_WIDGET", options)).toHaveLength(1);
    // The fixture's refused words are the server's no longer.
    expect(placeholderErrors("MY_WIDGET_FILE", options)).toEqual([]);
  });

  it("takes the reserved names and prefixes from the server, for every context", () => {
    const reserved = {
      reserved_names: ["MY_RESERVED"],
      reserved_prefixes: ["NOPE_"],
    };
    for (const base of [personal, operator]) {
      const options = optionsWith({}, reserved, base);
      expect(placeholderErrors("MY_RESERVED", options)).toEqual([
        "Placeholder name MY_RESERVED is reserved",
      ]);
      expect(placeholderErrors("NOPE_TOKEN", options)).toEqual([
        "Placeholder name NOPE_TOKEN starts with reserved prefix NOPE_",
      ]);
    }
    // PATH is reserved only because the fixture says so.
    expect(
      placeholderErrors("PATH", optionsWith({}, reserved, operator)),
    ).toEqual([]);
  });

  it("matches the name pattern from the server against the whole name", () => {
    const options = optionsWith({}, { name_pattern: "[A-Z]{2}_KEY" });
    expect(placeholderErrors("MY_KEY", options)).toEqual([]);
    for (const name of ["ABC_KEY", "MY_KEY_ID", "_MY_KEY"])
      expect(placeholderErrors(name, options)).toEqual([
        `${name} is not allowed as a placeholder: name a credential, such as MY_API_KEY, with TOKEN, KEY, SECRET, PASSWORD, PAT, AUTH, or CREDENTIAL as one of its words.`,
      ]);
  });

  it("leaves a name pattern it cannot read to the server, and still checks the words", () => {
    const options = optionsWith({}, { name_pattern: "(" });
    // The server's own pattern refuses a leading underscore.
    expect(placeholderErrors("_MY_KEY", options)).toEqual([]);
    expect(placeholderErrors("MY_NAME", options)).toEqual([
      "MY_NAME is not allowed as a placeholder: name a credential, such as MY_API_KEY, with TOKEN, KEY, SECRET, PASSWORD, PAT, AUTH, or CREDENTIAL as one of its words.",
    ]);
  });

  it("matches the value pattern against the whole value", () => {
    const options = optionsWith(
      {},
      { value_pattern: "[a-z]{1,3}", value_max_length: 3 },
    );
    expect(placeholderErrors("MY_API_KEY", options, "abc")).toEqual([]);
    for (const value of ["abcd", "xabc", "abc1", "ab\n", ""])
      expect(placeholderErrors("MY_API_KEY", options, value)).toEqual([
        "The value of MY_API_KEY must be 1 to 3 letters, digits, or . _ : -",
      ]);
  });

  it("leaves a value pattern it cannot read to the server", () => {
    const options = optionsWith({}, { value_pattern: "(" });
    expect(
      placeholderErrors("MY_API_KEY", options, "any value at all"),
    ).toEqual([]);
  });

  describe("without the server's validation, when the presets could not be loaded", () => {
    it("skips the caps on rules and size", () => {
      const rules = Array.from({ length: 51 }, validRule);
      expect(errorsFor(null, { rules }, withoutValidation)).toEqual([]);
      const description = "x".repeat(17 * 1024);
      expect(
        errorsFor(
          null,
          { rules: [validRule()], description },
          withoutValidation,
        ),
      ).toEqual([]);
      expect(
        errorsFor(
          null,
          { rules: Array.from({ length: 51 }, validRule), restrictOnly: true },
          withoutValidation,
        ),
      ).toEqual([]);
    });

    it("skips the reserved names and the personal placeholder rules", () => {
      expect(placeholderErrors("PATH", withoutValidation)).toEqual([]);
      expect(placeholderErrors("MINDROOM_TOKEN", withoutValidation)).toEqual(
        [],
      );
      expect(placeholderErrors("LD_PRELOAD", withoutValidation)).toEqual([]);
      expect(
        placeholderErrors("MY_API_KEY", withoutValidation, "has space"),
      ).toEqual([]);
      // A name no server accepts is left to the server too, since its words are not known.
      expect(placeholderErrors("lowercase", withoutValidation)).toEqual([]);
    });

    it("still checks what needs no validation from the server", () => {
      expect(
        placeholderErrors("lowercase", { ...operator, validation: null }),
      ).toEqual(["Placeholder name lowercase must match ^[A-Z_][A-Z0-9_]*$"]);
      expect(
        errorsFor(
          { preset: "github" },
          {
            placeholders: [
              { name: "MY_API_KEY", value: "a" },
              { name: "MY_API_KEY", value: "b" },
            ],
          },
          withoutValidation,
        ),
      ).toEqual(["Placeholder MY_API_KEY is listed twice"]);
    });
  });
});
