import type {
  AuthoredEgressService,
  EgressAuth,
  EgressAuthType,
  EgressCredentialService,
  EgressPreset,
  EgressRule,
  EgressRuleSummary,
} from "./types";

/**
 * The editor's data model, apart from any rendering: the form state, how it
 * maps to and from a service as authored, what stops it from being saved, the
 * GitHub repository rules, and whether another service already covers a rule.
 */

/** Where the editor runs: the personal page, or the dashboard writing config.yaml. */
export type EditorContext = "personal" | "operator";

export interface EditorOptions {
  context: EditorContext;
  /** Shared and unscoped agents cannot use personal accounts, so OAuth is off. */
  sharedTarget: boolean;
  /** The server's presets; `null` while they load, which a shared target treats as "may carry a login". */
  presets: readonly EgressPreset[] | null;
}

export const DEFAULT_TEMPLATE = "{secret}";
export const BROKERED_PLACEHOLDER = "mindroom-brokered";
export const MAX_RULES = 50;
const MAX_SERVICE_BYTES = 16 * 1024;
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const PROVIDER_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
const HEADER_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
// Headers that frame the message, route it, or authenticate to the proxy belong to the broker.
const RESERVED_HEADERS = new Set([
  "host",
  "content-length",
  "transfer-encoding",
  "connection",
  "upgrade",
  "te",
  "trailer",
  "proxy-authorization",
  "proxy-connection",
  "keep-alive",
]);
const PLACEHOLDER_NAME_PATTERN = /^[A-Z_][A-Z0-9_]*$/;
// Services of a personal scope may only set credential-like placeholders with plain values.
const PERSONAL_PLACEHOLDER_NAME = /^[A-Z][A-Z0-9_]*$/;
const PERSONAL_PLACEHOLDER_VALUE = /^[A-Za-z0-9._:-]{1,256}$/;
const PERSONAL_PLACEHOLDER_WORDS = [
  "AUTH",
  "CREDENTIAL",
  "KEY",
  "PASSWORD",
  "PAT",
  "SECRET",
  "TOKEN",
];

const GITHUB_API_HOST = "api.github.com";
const GITHUB_WEB_HOST = "github.com";
const GITHUB_UPLOADS_HOST = "uploads.github.com";
const GITHUB_HOSTS = [GITHUB_API_HOST, GITHUB_WEB_HOST, GITHUB_UPLOADS_HOST];
const GITHUB_REPOSITORY_LINE =
  /^([A-Za-z0-9][A-Za-z0-9-]*)\/(\*|[A-Za-z0-9._-]+)$/;
const GITHUB_MAX_OWNER_LENGTH = 39;
const GITHUB_MAX_REPOSITORY_LENGTH = 100;

export interface RuleForm {
  host: string;
  port: string;
  pathPrefix: string;
  authType: EgressAuthType;
  username: string;
  headerOrParam: string;
  template: string;
}

export interface PlaceholderForm {
  name: string;
  value: string;
}

/**
 * Fields the loaded service set to an empty value on purpose. A preset service
 * can drop the preset's placeholders with `placeholder_env: {}`, so saving the
 * form unchanged has to write that back instead of leaving the key out.
 */
interface ExplicitlyEmpty {
  displayName: "" | null | undefined;
  description: boolean;
  placeholders: boolean;
}

/** What the editor shows; `serializeService` turns it into the authored service. */
export interface EgressServiceForm {
  name: string;
  /** Empty for a custom service. */
  preset: string;
  displayName: string;
  description: string;
  /** With a preset: replace its rules by the ones below. */
  replacePresetRules: boolean;
  rules: RuleForm[];
  placeholders: PlaceholderForm[];
  restrictToRules: boolean;
  oauthProvider: string;
  explicitlyEmpty: ExplicitlyEmpty;
}

export function emptyRule(): RuleForm {
  return {
    host: "",
    port: "",
    pathPrefix: "",
    authType: "bearer",
    username: "",
    headerOrParam: "",
    template: "",
  };
}

export function ruleFormFrom(rule: EgressRule): RuleForm {
  return {
    host: rule.host,
    port: rule.port === undefined ? "" : String(rule.port),
    pathPrefix: rule.path_prefix ?? "",
    authType: rule.auth.type,
    username: rule.auth.username ?? "",
    headerOrParam: rule.auth.name ?? "",
    template: rule.auth.template ?? "",
  };
}

/** Build the form for an authored service, or an empty custom service for `null`. */
export function formFromService(
  name: string,
  service: AuthoredEgressService | null,
): EgressServiceForm {
  const preset = service?.preset ?? "";
  const rules = (service?.rules ?? []).map(ruleFormFrom);
  const replacePresetRules = preset !== "" && rules.length > 0;
  const displayName = service?.display_name;
  return {
    name,
    preset,
    displayName: displayName ?? "",
    description: service?.description ?? "",
    replacePresetRules,
    rules: preset === "" && rules.length === 0 ? [emptyRule()] : rules,
    placeholders: Object.entries(service?.placeholder_env ?? {}).map(
      ([placeholderName, value]) => ({ name: placeholderName, value }),
    ),
    restrictToRules: service?.restrict_to_rules ?? false,
    oauthProvider: service?.oauth_provider ?? "",
    explicitlyEmpty: {
      displayName:
        displayName === null || displayName === "" ? displayName : undefined,
      description: service?.description === "",
      placeholders:
        service?.placeholder_env !== undefined &&
        Object.keys(service.placeholder_env).length === 0,
    },
  };
}

export function presetOf(
  presets: readonly EgressPreset[] | null,
  id: string,
): EgressPreset | undefined {
  return presets?.find((preset) => preset.id === id);
}

export function usesOwnRules(form: EgressServiceForm): boolean {
  return form.preset === "" || form.replacePresetRules;
}

function ruleToAuthored(rule: RuleForm): EgressRule {
  const auth: EgressAuth = { type: rule.authType };
  if (rule.authType === "basic") auth.username = rule.username.trim();
  if (rule.authType === "header" || rule.authType === "query") {
    auth.name = rule.headerOrParam.trim();
    if (rule.template !== "" && rule.template !== DEFAULT_TEMPLATE)
      auth.template = rule.template;
  }
  const authored: EgressRule = { host: rule.host.trim().toLowerCase(), auth };
  if (rule.port.trim() !== "") authored.port = Number(rule.port);
  const pathPrefix = rule.pathPrefix.trim();
  if (pathPrefix !== "" && pathPrefix !== "/")
    authored.path_prefix = pathPrefix;
  return authored;
}

/**
 * Turn the form into the service as authored.
 *
 * A preset service keeps `{preset: id}` and only the fields the user set,
 * because the server expands the preset and later preset updates still apply.
 * `initial` supplies what the form cannot show: the operator-only
 * `oauth_on_shared_workers`, and an OAuth provider override on the same preset.
 * Fields the loaded service emptied on purpose stay empty, so an unchanged
 * save writes the service back as it was.
 */
export function serializeService(
  form: EgressServiceForm,
  options: EditorOptions,
  initial: AuthoredEgressService | null = null,
): AuthoredEgressService {
  const authored: AuthoredEgressService = {};
  if (form.preset) authored.preset = form.preset;
  if (form.displayName.trim()) authored.display_name = form.displayName.trim();
  else if (form.explicitlyEmpty.displayName !== undefined)
    authored.display_name = form.explicitlyEmpty.displayName;
  if (form.description.trim()) authored.description = form.description.trim();
  else if (form.explicitlyEmpty.description) authored.description = "";
  if (usesOwnRules(form)) authored.rules = form.rules.map(ruleToAuthored);
  const placeholders = form.placeholders.filter(
    (placeholder) => placeholder.name.trim() !== "",
  );
  if (placeholders.length > 0)
    authored.placeholder_env = Object.fromEntries(
      placeholders.map((placeholder) => [
        placeholder.name.trim(),
        placeholder.value,
      ]),
    );
  else if (form.explicitlyEmpty.placeholders) authored.placeholder_env = {};
  if (form.preset === "") {
    if (!options.sharedTarget && form.oauthProvider.trim())
      authored.oauth_provider = form.oauthProvider.trim();
  } else if (options.sharedTarget && presetHasLogin(form.preset, options)) {
    // A shared agent cannot use personal accounts, so the preset's login is dropped.
    authored.oauth_provider = null;
  } else if (
    initial?.oauth_provider !== undefined &&
    form.preset === (initial.preset ?? "")
  ) {
    // An override written for this preset; another preset has its own login.
    authored.oauth_provider = initial.oauth_provider;
  }
  if (form.restrictToRules) authored.restrict_to_rules = true;
  if (
    options.context === "operator" &&
    initial?.oauth_on_shared_workers !== undefined
  )
    authored.oauth_on_shared_workers = initial.oauth_on_shared_workers;
  return authored;
}

/** Whether a preset carries an account login; unknown while the presets are not loaded, so assume it may. */
export function presetHasLogin(
  presetId: string,
  options: Pick<EditorOptions, "presets">,
): boolean {
  if (options.presets === null) return true;
  return presetOf(options.presets, presetId)?.oauth_provider != null;
}

function hostError(host: string): string | null {
  const value = host.trim().toLowerCase();
  if (!value) return "host is required";
  if (value.includes("://")) return "host must not contain a scheme";
  if (value.includes(":"))
    return "host must not contain a port (use the port field)";
  if (value.includes("/"))
    return "host must not contain a path (use the path prefix field)";
  if (value.startsWith("*.")) {
    const parts = value.split(".");
    if (parts.filter((part) => part === "*").length > 1)
      return "host can only have one wildcard label";
    if (parts.length < 3)
      return "wildcard host must have at least one domain part after *.";
  } else if (value.includes("*")) {
    return "wildcard must be a single leading *. label";
  }
  return null;
}

function templateError(template: string): string | null {
  if (template === "") return null;
  return template.split(DEFAULT_TEMPLATE).length - 1 === 1
    ? null
    : "template must contain {secret} exactly once";
}

function ruleErrors(rule: RuleForm): string[] {
  const errors: string[] = [];
  const hostProblem = hostError(rule.host);
  if (hostProblem) errors.push(hostProblem);
  const pathPrefix = rule.pathPrefix.trim();
  if (pathPrefix !== "" && !pathPrefix.startsWith("/"))
    errors.push("path prefix must start with /");
  const port = rule.port.trim();
  if (port !== "") {
    const number = Number(port);
    if (!Number.isInteger(number) || number < 1 || number > 65535)
      errors.push("port must be a number from 1 to 65535");
  }
  if (rule.authType === "basic" && !rule.username.trim())
    errors.push("basic auth needs a username");
  if (rule.authType === "header") {
    const name = rule.headerOrParam.trim();
    if (!name) errors.push("header auth needs a header name");
    else if (!HEADER_NAME_PATTERN.test(name))
      errors.push("header name must be a valid HTTP header name");
    else if (RESERVED_HEADERS.has(name.toLowerCase()))
      errors.push(`header name ${name} is reserved for the broker`);
  }
  if (rule.authType === "query" && !rule.headerOrParam.trim())
    errors.push("query auth needs a parameter name");
  if (rule.authType === "header" || rule.authType === "query") {
    const problem = templateError(rule.template);
    if (problem) errors.push(problem);
  }
  return errors;
}

function personalPlaceholderError(name: string, value: string): string | null {
  const words = name.split("_");
  if (
    !PERSONAL_PLACEHOLDER_NAME.test(name) ||
    !PERSONAL_PLACEHOLDER_WORDS.some((word) => words.includes(word))
  )
    return `placeholder name ${name} must match ^[A-Z][A-Z0-9_]*$ and have one of ${PERSONAL_PLACEHOLDER_WORDS.join(", ")} between underscores`;
  if (!PERSONAL_PLACEHOLDER_VALUE.test(value))
    return `placeholder value of ${name} must be 1 to 256 characters from A-Z, a-z, 0-9, and . _ : -`;
  return null;
}

/** Return what stops the form from being saved, in reading order; empty when it can be. */
export function validateService(
  form: EgressServiceForm,
  options: EditorOptions,
  settings: { editing: boolean; takenNames: readonly string[] },
): string[] {
  const errors: string[] = [];
  if (!settings.editing) {
    const name = form.name;
    if (!NAME_PATTERN.test(name))
      errors.push(
        "Name must start with a lowercase letter or digit and use only lowercase letters, digits, - and _ (63 characters at most)",
      );
    else if (/_oauth(_client)?$/.test(name))
      errors.push("Name must not end in _oauth or _oauth_client");
    else if (settings.takenNames.includes(name))
      errors.push(`A service named ${name} already exists`);
  }
  if (
    form.preset === "" &&
    !options.sharedTarget &&
    form.oauthProvider.trim() &&
    !PROVIDER_PATTERN.test(form.oauthProvider.trim())
  )
    errors.push(
      "Account login must be a provider id such as github or google_drive (lowercase letters, digits, - and _)",
    );
  if (usesOwnRules(form)) {
    if (form.rules.length === 0) errors.push("Add at least one rule");
    if (options.context === "personal" && form.rules.length > MAX_RULES)
      errors.push(`A service can have at most ${MAX_RULES} rules`);
    form.rules.forEach((rule, index) => {
      for (const problem of ruleErrors(rule))
        errors.push(`Rule ${index + 1}: ${problem}`);
    });
  }
  const seen = new Set<string>();
  for (const placeholder of form.placeholders) {
    const name = placeholder.name.trim();
    // A row without a name is an unused row, whatever its value.
    if (name === "") continue;
    if (!PLACEHOLDER_NAME_PATTERN.test(name)) {
      errors.push(`Placeholder name ${name} must match ^[A-Z_][A-Z0-9_]*$`);
      continue;
    }
    if (seen.has(name)) errors.push(`Placeholder ${name} is listed twice`);
    seen.add(name);
    const personalProblem =
      options.context === "personal"
        ? personalPlaceholderError(name, placeholder.value)
        : null;
    if (personalProblem) errors.push(personalProblem);
  }
  if (
    options.context === "personal" &&
    errors.length === 0 &&
    new TextEncoder().encode(JSON.stringify(serializeService(form, options)))
      .length > MAX_SERVICE_BYTES
  )
    errors.push(`A service can take at most ${MAX_SERVICE_BYTES / 1024} KiB`);
  return errors;
}

/**
 * Rules that let a service reach only the listed GitHub repositories.
 *
 * Each line is `owner/repo`, or `owner/*` for everything the owner has. A
 * repository gets `api.github.com` `/repos/owner/repo` and `github.com`
 * `/owner/repo` and `/owner/repo.git`; an owner gets the same under a trailing
 * slash, which matches every path below it. `uploads.github.com` (release
 * assets, limited to the same repositories) and `/graphql` (every repository
 * the key can reach) are added only when asked for.
 */
export function githubRepositoryRules(
  input: string,
  options: { uploads: boolean; graphql: boolean },
): { rules: EgressRule[]; errors: string[] } {
  const bearer: EgressAuth = { type: "bearer" };
  const git: EgressAuth = { type: "basic", username: "x-access-token" };
  const errors: string[] = [];
  const targets = new Map<string, { owner: string; repo: string }>();
  for (const line of input.split(/\r?\n/)) {
    const text = line.trim();
    if (!text) continue;
    const match = GITHUB_REPOSITORY_LINE.exec(text);
    // GitHub never has a repository named `.git`, so a pasted clone name loses its suffix.
    const repo = match?.[2].replace(/\.git$/, "");
    if (!match || !repo || repo === "." || repo === "..") {
      errors.push(`${text} is not owner/repo or owner/*`);
      continue;
    }
    if (
      match[1].length > GITHUB_MAX_OWNER_LENGTH ||
      repo.length > GITHUB_MAX_REPOSITORY_LENGTH
    ) {
      errors.push(
        `${text} is too long: GitHub owners have at most ${GITHUB_MAX_OWNER_LENGTH} characters and repositories ${GITHUB_MAX_REPOSITORY_LENGTH}`,
      );
      continue;
    }
    targets.set(`${match[1]}/${repo}`, { owner: match[1], repo });
  }
  if (targets.size === 0 && errors.length === 0)
    errors.push("Enter at least one repository, such as owner/repo");
  if (errors.length > 0) return { rules: [], errors };
  const rules: EgressRule[] = [];
  for (const { owner, repo } of targets.values()) {
    if (repo === "*") {
      rules.push(
        {
          host: GITHUB_API_HOST,
          path_prefix: `/repos/${owner}/`,
          auth: bearer,
        },
        { host: GITHUB_WEB_HOST, path_prefix: `/${owner}/`, auth: git },
      );
      if (options.uploads)
        rules.push({
          host: GITHUB_UPLOADS_HOST,
          path_prefix: `/repos/${owner}/`,
          auth: bearer,
        });
      continue;
    }
    rules.push(
      {
        host: GITHUB_API_HOST,
        path_prefix: `/repos/${owner}/${repo}`,
        auth: bearer,
      },
      { host: GITHUB_WEB_HOST, path_prefix: `/${owner}/${repo}`, auth: git },
      {
        host: GITHUB_WEB_HOST,
        path_prefix: `/${owner}/${repo}.git`,
        auth: git,
      },
    );
    if (options.uploads)
      rules.push({
        host: GITHUB_UPLOADS_HOST,
        path_prefix: `/repos/${owner}/${repo}`,
        auth: bearer,
      });
  }
  if (options.graphql)
    rules.push({
      host: GITHUB_API_HOST,
      path_prefix: "/graphql",
      auth: bearer,
    });
  return { rules, errors };
}

/** The part of a listed service that decides whether it covers another rule. */
export type ServiceCoverage = Pick<
  EgressCredentialService,
  "name" | "display_name" | "rules"
>;

/**
 * Whether `broader` matches every path `narrower` does, as the broker matches
 * them: a prefix ending in `/` covers by plain prefix, any other covers the
 * exact path and what follows a `/`.
 */
function prefixCovers(broader: string, narrower: string): boolean {
  if (broader.endsWith("/")) return narrower.startsWith(broader);
  return narrower === broader || narrower.startsWith(`${broader}/`);
}

/**
 * Name the other services that already cover one of `rules` with a shorter prefix.
 *
 * Limiting a service to some paths cannot narrow what another service on the
 * same host and port covers more widely: the broker still sends a request that
 * matches that service's rule with its credentials. A rule covers another when
 * it has the same host, the same port or none, and a shorter path prefix that
 * covers the other's. Services come in listing order and each is named once.
 */
export function findBroaderServices(
  rules: readonly EgressRuleSummary[],
  others: readonly ServiceCoverage[],
): string[] {
  const names: string[] = [];
  for (const other of others) {
    const covers = (other.rules ?? []).some((broader) =>
      rules.some(
        (rule) =>
          broader.host.toLowerCase() === rule.host.toLowerCase() &&
          (broader.port === null || broader.port === rule.port) &&
          broader.path_prefix.length < rule.path_prefix.length &&
          prefixCovers(broader.path_prefix, rule.path_prefix),
      ),
    );
    if (covers && !names.includes(other.display_name))
      names.push(other.display_name);
  }
  return names;
}

/** Where the form's service would apply: its own rules, or the preset's when it keeps them. */
export function formRuleSummaries(
  form: EgressServiceForm,
  presets: readonly EgressPreset[] | null,
): EgressRuleSummary[] {
  if (!usesOwnRules(form)) return presetOf(presets, form.preset)?.rules ?? [];
  return form.rules
    .filter((rule) => rule.host.trim() !== "")
    .map((rule) => {
      const port = Number(rule.port.trim());
      return {
        host: rule.host.trim().toLowerCase(),
        port: rule.port.trim() !== "" && Number.isInteger(port) ? port : null,
        path_prefix: rule.pathPrefix.trim() || "/",
      };
    });
}

/** Whether to offer the GitHub helper: a GitHub service, rules on GitHub hosts, or a service with no host yet. */
export function looksLikeGithub(form: EgressServiceForm): boolean {
  if (form.preset === "github") return true;
  if (!usesOwnRules(form)) return false;
  const onGithub = form.rules.some((rule) =>
    GITHUB_HOSTS.includes(rule.host.trim()),
  );
  if (form.preset === "")
    return onGithub || form.rules.every((rule) => rule.host.trim() === "");
  return onGithub;
}
