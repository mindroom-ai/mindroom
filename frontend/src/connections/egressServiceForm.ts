import { hasCustomTemplate } from "./egressRuleAuth";
import type {
  AuthoredEgressService,
  EgressAuth,
  EgressRuleAuthType,
  EgressValidation,
  EgressPreset,
  EgressRule,
} from "./types";

/**
 * The editor's form state and how it maps to and from a service as authored:
 * what the form holds, building it from a service, turning it back into the
 * authored service, and the few questions the editor asks of the form (which
 * name to suggest, whether More options has something to show).
 */

/** Where the editor runs: the personal page, or the dashboard writing config.yaml. */
export type EditorContext = "personal" | "operator";

export interface EditorOptions {
  context: EditorContext;
  /** Shared and unscoped agents cannot use personal accounts, so OAuth is off. */
  sharedTarget: boolean;
  /** The server's presets; `null` while they load, when the form cannot be saved yet. */
  presets: readonly EgressPreset[] | null;
  /**
   * What the server checks when a service is saved, from the presets
   * response; `null` while it loads and when the presets could not be loaded,
   * in which case the checks that need it are left to the server.
   */
  validation: EgressValidation | null;
}

export const BROKERED_PLACEHOLDER = "mindroom-brokered";

export interface RuleForm {
  host: string;
  port: string;
  pathPrefix: string;
  authType: EgressRuleAuthType;
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

/** No field emptied on purpose: what a new service, or one that picked another preset, starts with. */
export const NOT_EXPLICITLY_EMPTY: ExplicitlyEmpty = {
  displayName: undefined,
  description: false,
  placeholders: false,
};

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
  /** A service that only limits paths: no key, account, or placeholders, and rules that never pick a credential. */
  restrictOnly: boolean;
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
  // A rule of a service that only limits paths has no auth; the form shows the default.
  const auth = rule.auth ?? { type: "bearer" as const };
  return {
    host: rule.host,
    port: rule.port == null ? "" : String(rule.port),
    pathPrefix: rule.path_prefix ?? "",
    authType:
      auth.type === "header" && hasCustomTemplate(auth.template)
        ? "template"
        : auth.type,
    username: auth.username ?? "",
    headerOrParam: auth.name ?? "",
    template: auth.template ?? "",
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
    rules:
      preset === "" && rules.length === 0 && !service?.restrict_only
        ? [emptyRule()]
        : rules,
    placeholders: Object.entries(service?.placeholder_env ?? {}).map(
      ([placeholderName, value]) => ({ name: placeholderName, value }),
    ),
    restrictToRules: service?.restrict_to_rules ?? false,
    restrictOnly: service?.restrict_only ?? false,
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

/** Switch how a rule sends the key; plain Header sends the key as it is, so it drops a custom template. */
export function applyAuthChoice(
  rule: RuleForm,
  authType: EgressRuleAuthType,
): RuleForm {
  return {
    ...rule,
    authType,
    template: authType === "header" ? "" : rule.template,
  };
}

function ruleToAuthored(rule: RuleForm): EgressRule {
  const type = rule.authType === "template" ? "header" : rule.authType;
  const auth: EgressAuth = { type };
  if (type === "basic") auth.username = rule.username.trim();
  if (type === "header" || type === "query") {
    auth.name = rule.headerOrParam.trim();
    if (hasCustomTemplate(rule.template)) auth.template = rule.template;
  }
  const authored: EgressRule = { host: rule.host.trim().toLowerCase(), auth };
  if (rule.port.trim() !== "") authored.port = Number(rule.port);
  const pathPrefix = rule.pathPrefix.trim();
  if (pathPrefix !== "" && pathPrefix !== "/")
    authored.path_prefix = pathPrefix;
  return authored;
}

/**
 * The display name a new custom service gets while the form leaves it empty:
 * its website, as the person typed it, not the title-cased name the server
 * would derive. A preset keeps its own, which the server supplies, and so does
 * a service whose display name was emptied on purpose.
 */
function defaultDisplayName(form: EgressServiceForm): string {
  if (form.preset !== "" || form.explicitlyEmpty.displayName !== undefined)
    return "";
  return form.rules[0]?.host.trim().toLowerCase() ?? "";
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
  // A service that only limits paths has rules of its own, whatever preset the form once held.
  if (form.preset && !form.restrictOnly) authored.preset = form.preset;
  // A service that is edited keeps the display name it has, or none.
  const displayName =
    form.displayName.trim() ||
    (initial === null ? defaultDisplayName(form) : "");
  if (displayName) authored.display_name = displayName;
  else if (form.explicitlyEmpty.displayName !== undefined)
    authored.display_name = form.explicitlyEmpty.displayName;
  if (form.description.trim()) authored.description = form.description.trim();
  else if (form.explicitlyEmpty.description) authored.description = "";
  if (form.restrictOnly) {
    // It sends no key, so its rules carry no auth and there is no login or
    // placeholder to write; the restriction is what it is for.
    authored.rules = form.rules.map((rule) => {
      const { auth: _auth, ...withoutAuth } = ruleToAuthored(rule);
      return withoutAuth;
    });
    authored.restrict_to_rules = true;
    authored.restrict_only = true;
    return authored;
  }
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

/** Whether a preset carries an account login. */
export function presetHasLogin(
  presetId: string,
  options: Pick<EditorOptions, "presets">,
): boolean {
  return presetOf(options.presets, presetId)?.oauth_provider != null;
}

/**
 * What the Website field keeps of what was typed or pasted: the host, without
 * a scheme and cut at the first `/`, `?`, or `#`, and the path that was cut off
 * (up to a `?` or `#`), which belongs in the Path prefix. A copied URL thus
 * becomes its host plus a prefix.
 */
export function splitHostInput(value: string): { host: string; path: string } {
  const scheme = value.indexOf("://");
  const rest = scheme === -1 ? value : value.slice(scheme + 3);
  const end = rest.search(/[/?#]/);
  if (end === -1) return { host: rest, path: "" };
  const path = rest[end] === "/" ? rest.slice(end).split(/[?#]/)[0] : "";
  return { host: rest.slice(0, end), path };
}

export const MAX_NAME_LENGTH = 63;

/**
 * The name a new service gets until the user types one: the preset's id, or
 * the first label of the first rule's host (`httpbin.org` gives `httpbin`).
 * When `takenNames` has it, the first free `{base}-2` or `{base}-3` instead;
 * with all three taken it stays the base, so saving names the clash.
 */
export function suggestedName(
  form: EgressServiceForm,
  takenNames: readonly string[] = [],
): string {
  let base = form.preset;
  if (!base) {
    const host = (form.rules[0]?.host ?? "").trim().toLowerCase();
    const label = host.replace(/^\*\./, "").split(/[.:/]/)[0];
    base = label
      .replace(/[^a-z0-9_-]/g, "")
      .replace(/^[-_]+/, "")
      .slice(0, MAX_NAME_LENGTH);
  }
  if (base === "") return "";
  for (const suffix of ["", "-2", "-3"]) {
    const candidate = `${base.slice(0, MAX_NAME_LENGTH - suffix.length)}${suffix}`;
    if (!takenNames.includes(candidate)) return candidate;
  }
  return base;
}

/** Whether the form sets anything the basic fields do not show, so the editor opens More options. */
export function usesMoreOptions(form: EgressServiceForm): boolean {
  const first = form.rules[0];
  return (
    form.displayName.trim() !== "" ||
    form.description.trim() !== "" ||
    form.replacePresetRules ||
    form.rules.length > 1 ||
    (usesOwnRules(form) &&
      first !== undefined &&
      (first.pathPrefix.trim() !== "" || first.port.trim() !== "")) ||
    form.restrictToRules ||
    form.oauthProvider.trim() !== "" ||
    form.placeholders.length > 0
  );
}
