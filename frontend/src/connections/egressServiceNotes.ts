import { formatList } from "./accountCopy";
import { type EgressServiceForm, usesOwnRules } from "./egressServiceForm";
import { hostPort } from "./egressRowText";
import type {
  EgressCredentialService,
  EgressPreset,
  EgressRuleSummary,
  EgressScope,
} from "./types";

/**
 * The texts the editor shows about a service beside its fields: warnings about
 * placeholders and websites that another service of the agent also covers, and
 * the note on whose service this is.
 */

/** What to tell the user about a placeholder that an administrator's service also sets, or null. */
export function configPlaceholderWarning(
  name: string,
  configPlaceholders: Readonly<Record<string, string>>,
): string | null {
  const variable = name.trim();
  if (!Object.prototype.hasOwnProperty.call(configPlaceholders, variable))
    return null;
  return `${variable} is also set by ${configPlaceholders[variable]} from your administrator; when both are set, theirs is used.`;
}

/**
 * What to tell the user about the placeholders their preset sets that an
 * administrator's service also sets, one text per variable in the preset's
 * order. The preset's placeholders apply only while the service lists none of
 * its own and has not dropped them on purpose.
 */
export function presetPlaceholderWarnings(
  form: EgressServiceForm,
  preset: EgressPreset | undefined,
  configPlaceholders: Readonly<Record<string, string>>,
): string[] {
  if (
    !preset ||
    form.preset !== preset.id ||
    form.placeholders.length > 0 ||
    form.explicitlyEmpty.placeholders
  )
    return [];
  return Object.keys(preset.placeholder_env).flatMap((variable) => {
    const warning = configPlaceholderWarning(variable, configPlaceholders);
    return warning === null ? [] : [warning];
  });
}

/** Map each variable to the display name of the administrator's service that sets it, from a listing. */
export function configPlaceholderOwners(
  services: readonly Pick<
    EgressCredentialService,
    "source" | "display_name" | "placeholder_names"
  >[],
): Record<string, string> {
  const owners: Record<string, string> = {};
  for (const service of services) {
    if (service.source !== "config") continue;
    for (const name of service.placeholder_names ?? [])
      if (!Object.prototype.hasOwnProperty.call(owners, name))
        owners[name] = service.display_name;
  }
  return owners;
}

/** Where a rule applies, as the form, a preset, and a listing each hold it. */
interface RuleTarget {
  host: string;
  port?: number | null;
  path_prefix?: string | null;
}

/** A rule as a short host and path, such as `www.googleapis.com/drive/`. */
export function ruleCoverage(rule: EgressRuleSummary): string {
  const path = rule.path_prefix === "/" ? "" : rule.path_prefix;
  return `${hostPort(rule)}${path}`;
}

/**
 * Whether two rules can match the same request: the same host, ports that
 * agree (a missing port matches any), and one path prefix covering the other
 * by whole segments (an empty prefix covers the whole host), so `/gmail`
 * covers `/gmail/x` but not `/gmailx`.
 */
export function rulesOverlap(a: RuleTarget, b: RuleTarget): boolean {
  if (a.host.toLowerCase() !== b.host.toLowerCase()) return false;
  if (a.port != null && b.port != null && a.port !== b.port) return false;
  const [x, y] = [a, b].map((rule) =>
    (rule.path_prefix ?? "").replace(/\/$/, ""),
  );
  return (
    x === "" ||
    y === "" ||
    x === y ||
    x.startsWith(`${y}/`) ||
    y.startsWith(`${x}/`)
  );
}

/**
 * What to tell the user when another service of the agent already sends a
 * credential where this form does: one text per such service, with each of its
 * addresses that overlaps one of the form's. Services that only limit paths
 * send nothing, and the service being edited does not compete with itself.
 */
export function overlapWarnings(
  form: EgressServiceForm,
  preset: EgressPreset | undefined,
  others: readonly Pick<
    EgressCredentialService,
    "name" | "display_name" | "rules" | "restrict_only"
  >[],
  editingName: string | undefined,
): string[] {
  const covered: RuleTarget[] = (
    usesOwnRules(form)
      ? form.rules.map((rule) => ({
          host: rule.host.trim(),
          port: rule.port.trim() === "" ? null : Number(rule.port),
          path_prefix: rule.pathPrefix.trim(),
        }))
      : (preset?.rules ?? [])
  ).filter((rule) => rule.host !== "");
  return others.flatMap((other) => {
    if (other.restrict_only || other.name === editingName) return [];
    const shared = [
      ...new Set(
        other.rules
          .filter(
            (rule) =>
              rule.auth_type !== null &&
              covered.some((mine) => rulesOverlap(mine, rule)),
          )
          .map(ruleCoverage),
      ),
    ];
    return shared.length > 0
      ? [
          `${other.display_name} already handles ${formatList(shared)} for this agent, so requests there may use it instead of this service.`,
        ]
      : [];
  });
}

/**
 * Who uses a personal service, for the note under the form's title; null where
 * nothing needs saying. A service whose form asks which account it uses
 * (`account`) has that account instead of a key.
 */
export function scopeNote(
  scope: EgressScope | undefined,
  agent: string,
  scopeAgents: readonly string[],
  account = false,
): string | null {
  const credential = account ? "account" : "key";
  if (scope === "user_agent")
    return `Only your ${agent} uses this service and its ${credential}.`;
  if (scope === "user")
    return `All your agents that share your personal workspace use this service and its ${credential}: ${scopeAgents.join(", ")}.`;
  return null;
}
