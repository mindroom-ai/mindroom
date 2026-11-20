import { headerNameError, templateError } from "./egressRuleAuth";
import {
  type EditorOptions,
  type EgressServiceForm,
  MAX_NAME_LENGTH,
  type PlaceholderForm,
  type RuleForm,
  serializeService,
  usesOwnRules,
} from "./egressServiceForm";
import {
  PLACEHOLDER_NAME_PATTERN,
  operatorPlaceholderNameError,
  personalPlaceholderError,
  personalPlaceholderNameError,
  reservedPlaceholderError,
} from "./egressPlaceholderRules";
import type { AuthoredEgressService } from "./types";

/**
 * What stops a form from being saved, and which field shows each problem.
 * `validateService` returns the problems in reading order; the field ids tell
 * the editor where to put them.
 */

// Service names (also capped at `MAX_NAME_LENGTH`) and account provider ids.
const ID_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;

function hostError(host: string): string | null {
  const value = host.trim().toLowerCase();
  if (!value) return "Host is required";
  if (value.includes(":"))
    return "Host must not contain a port (use the port field)";
  if (value.startsWith("*.")) {
    const parts = value.split(".");
    if (parts.filter((part) => part === "*").length > 1)
      return "Host can only have one wildcard label";
    if (parts.length < 3)
      return "Wildcard host must have at least one domain part after *.";
  } else if (value.includes("*")) {
    return "Wildcard must be a single leading *. label";
  }
  return null;
}

/** The part of a rule a problem belongs to, so the editor can show it by that field. */
export type RuleFieldPart =
  "host" | "pathPrefix" | "port" | "username" | "authName" | "template";

/** The id of the field that shows a problem with a rule. */
export function ruleField(index: number, part: RuleFieldPart): string {
  return `rule:${index}:${part}`;
}

/** The id of the field that shows a problem with a placeholder row. */
export function placeholderField(
  index: number,
  part: "name" | "value",
): string {
  return `placeholder:${index}:${part}`;
}

/**
 * The parts of the first rule the basic form shows: the website, how the key is
 * sent, and what that way needs. Path prefix and port sit under More options.
 */
export const BASIC_RULE_PARTS: readonly RuleFieldPart[] = [
  "host",
  "username",
  "authName",
  "template",
];

const BASIC_FIELDS: ReadonlySet<string> = new Set([
  "name",
  ...BASIC_RULE_PARTS.map((part) => ruleField(0, part)),
]);

/** Whether the basic form shows the field of a problem; any other opens More options. */
export function isBasicField(field: string): boolean {
  return BASIC_FIELDS.has(field);
}

/**
 * What stops a form from being saved, and where to say so. `field` is `name`,
 * `rules`, `oauthProvider`, a `ruleField`, or a `placeholderField`; `form`
 * means the service as a whole, which no single field shows.
 */
export interface ServiceProblem {
  field: string;
  message: string;
}

function ruleProblems(rule: RuleForm, index: number): ServiceProblem[] {
  const problems: ServiceProblem[] = [];
  const add = (part: RuleFieldPart, message: string | null) => {
    if (message) problems.push({ field: ruleField(index, part), message });
  };
  add("host", hostError(rule.host));
  const pathPrefix = rule.pathPrefix.trim();
  if (pathPrefix !== "" && !pathPrefix.startsWith("/"))
    add("pathPrefix", "Path prefix must start with /");
  const port = rule.port.trim();
  if (port !== "") {
    const number = Number(port);
    if (!Number.isInteger(number) || number < 1 || number > 65535)
      add("port", "Port must be a number from 1 to 65535");
  }
  if (rule.authType === "basic" && !rule.username.trim())
    add("username", "Basic auth needs a username");
  if (rule.authType === "header" || rule.authType === "template")
    add("authName", headerNameError(rule.headerOrParam.trim()));
  if (rule.authType === "query" && !rule.headerOrParam.trim())
    add("authName", "Query auth needs a parameter name");
  if (
    rule.authType === "header" ||
    rule.authType === "query" ||
    rule.authType === "template"
  )
    add("template", templateError(rule.template, rule.authType === "template"));
  return problems;
}

/**
 * The size the server counts for a service: its JSON with every character
 * outside printable ASCII written as a 6 byte `\uXXXX` escape, which is how
 * Python's `json.dumps` writes it. The server measures its own dump of the
 * validated service, so a few bytes of difference remain possible at the edge.
 */
function serverJsonSize(value: unknown): number {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, "\\u0000").length;
}

function nameProblem(
  name: string,
  settings: { takenNames: readonly string[] },
): string | null {
  if (name === "") return "Name is required";
  if (!ID_PATTERN.test(name) || name.length > MAX_NAME_LENGTH)
    return `Name must start with a lowercase letter or digit and use only lowercase letters, digits, - and _ (${MAX_NAME_LENGTH} characters at most)`;
  if (/_oauth(_client)?$/.test(name))
    return "Name must not end in _oauth or _oauth_client";
  if (settings.takenNames.includes(name))
    return `A service named ${name} already exists`;
  return null;
}

/** What is wrong with the placeholder rows, each at its row; a row without a name is an unused row. */
function placeholderProblems(
  placeholders: readonly PlaceholderForm[],
  options: EditorOptions,
): ServiceProblem[] {
  const problems: ServiceProblem[] = [];
  // Without the server's rules the checks that need them are left to the server.
  const placeholderRules = options.validation?.placeholder ?? null;
  const seen = new Set<string>();
  placeholders.forEach((placeholder, index) => {
    const name = placeholder.name.trim();
    // A row without a name is an unused row, whatever its value.
    if (name === "") return;
    const add = (part: "name" | "value", message: string) =>
      problems.push({ field: placeholderField(index, part), message });
    if (!PLACEHOLDER_NAME_PATTERN.test(name)) {
      // A personal user hears what the server would say, not a pattern.
      const message =
        options.context === "personal"
          ? placeholderRules &&
            personalPlaceholderNameError(name, placeholderRules)
          : operatorPlaceholderNameError(name);
      if (message) add("name", message);
      return;
    }
    const reservedProblem =
      placeholderRules && reservedPlaceholderError(name, placeholderRules);
    if (reservedProblem) {
      add("name", reservedProblem);
      return;
    }
    if (seen.has(name)) add("name", `Placeholder ${name} is listed twice`);
    seen.add(name);
    const personalProblem =
      options.context === "personal" && placeholderRules
        ? personalPlaceholderError(name, placeholder.value, placeholderRules)
        : null;
    if (personalProblem) add(personalProblem.part, personalProblem.message);
  });
  return problems;
}

/** Return what stops the form from being saved, in reading order; empty when it can be. */
export function validateService(
  form: EgressServiceForm,
  options: EditorOptions,
  settings: {
    editing: boolean;
    takenNames: readonly string[];
    /** The service the form opened with, which the save serializes the form against; null for a new one. */
    service: AuthoredEgressService | null;
  },
): ServiceProblem[] {
  const problems: ServiceProblem[] = [];
  if (!settings.editing) {
    const message = nameProblem(form.name, settings);
    if (message) problems.push({ field: "name", message });
  }
  if (form.restrictOnly) {
    // The repository helper writes the rules and tells when there are none,
    // and the server checks them again, but a long list can still pass the
    // caps on a service's size. The form has no field for those, so they
    // are told in its alert. A limit the helper cannot write has its rules in
    // the rule fields, which are checked like a service's, or takes them from
    // a preset, which has some.
    if (usesOwnRules(form)) {
      if (form.rules.length === 0)
        problems.push({
          field: "rules",
          message: "Enter at least one repository, such as owner/repo",
        });
      form.rules.forEach((rule, index) =>
        problems.push(...ruleProblems(rule, index)),
      );
    }
    const tooManyRules = tooManyRulesProblem(form, options);
    if (tooManyRules) problems.push({ field: "form", message: tooManyRules });
    problems.push(...sizeProblems(form, options, settings.service, problems));
    return problems;
  }
  if (
    form.preset === "" &&
    !options.sharedTarget &&
    form.oauthProvider.trim() &&
    !ID_PATTERN.test(form.oauthProvider.trim())
  )
    problems.push({
      field: "oauthProvider",
      message:
        "Account login must be a provider id such as github or google_drive (lowercase letters, digits, - and _)",
    });
  if (usesOwnRules(form)) {
    if (form.rules.length === 0)
      problems.push({ field: "rules", message: "Add at least one rule" });
    const tooManyRules = tooManyRulesProblem(form, options);
    if (tooManyRules) problems.push({ field: "rules", message: tooManyRules });
    form.rules.forEach((rule, index) =>
      problems.push(...ruleProblems(rule, index)),
    );
  }
  problems.push(...placeholderProblems(form.placeholders, options));
  problems.push(...sizeProblems(form, options, settings.service, problems));
  return problems;
}

/** What to say when a personal service has more rules than the server takes, or null; the dashboard has no such cap. */
function tooManyRulesProblem(
  form: EgressServiceForm,
  options: EditorOptions,
): string | null {
  const max = options.validation?.max_rules_per_service;
  return options.context === "personal" &&
    max !== undefined &&
    form.rules.length > max
    ? `A service can have at most ${max} rules`
    : null;
}

/** The size problem of a personal service, measured only once nothing else is wrong with it, as the save serializes it. */
function sizeProblems(
  form: EgressServiceForm,
  options: EditorOptions,
  service: AuthoredEgressService | null,
  found: readonly ServiceProblem[],
): ServiceProblem[] {
  const max = options.validation?.max_service_bytes;
  return options.context === "personal" &&
    max !== undefined &&
    found.length === 0 &&
    serverJsonSize(serializeService(form, options, service)) > max
    ? [
        {
          field: "form",
          message: `A service can take at most ${max / 1024} KiB`,
        },
      ]
    : [];
}
