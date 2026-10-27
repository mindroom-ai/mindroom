import { formatList } from "./accountCopy";
import { type EgressServiceForm, usesOwnRules } from "./egressServiceForm";
import type {
  EgressCredentialService,
  EgressPreset,
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

/**
 * What to tell the user when another service of the agent already sends a key
 * to a website this form covers: one text per such service, with all the
 * hosts it shares, and the broker's order for picking between them. Services
 * that only limit paths send no key, and the service being edited does not
 * compete with itself.
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
  const covered = new Set(
    (usesOwnRules(form) ? form.rules : (preset?.rules ?? []))
      .map((rule) => rule.host.trim().toLowerCase())
      .filter((host) => host !== ""),
  );
  if (covered.size === 0) return [];
  return others.flatMap((other) => {
    if (other.restrict_only || other.name === editingName) return [];
    const shared = [
      ...new Set(
        other.rules
          .filter((rule) => rule.auth_type !== null)
          .map((rule) => rule.host.toLowerCase())
          .filter((host) => covered.has(host)),
      ),
    ];
    return shared.length > 0
      ? [
          `${other.display_name} already sends a key to ${formatList(shared)} for this agent. An exact host wins, then a specific port, then the longest path, then your administrator's services before your own, then the service name alphabetically.`,
        ]
      : [];
  });
}

/** Who uses a personal service, for the note under the form's title; null where nothing needs saying. */
export function scopeNote(
  scope: EgressScope | undefined,
  agent: string,
  scopeAgents: readonly string[],
): string | null {
  if (scope === "user_agent")
    return `Only your ${agent} uses this service and its key.`;
  if (scope === "user")
    return `All your agents that share your personal workspace use this service and its key: ${scopeAgents.join(", ")}.`;
  return null;
}
