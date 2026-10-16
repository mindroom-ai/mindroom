import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect } from "vitest";
import egressPresets from "@/test/fixtures/egressPresets.json";
import {
  type EditorOptions,
  type EgressServiceForm,
  emptyRule,
  formFromService,
  serializeService,
} from "@/connections/egressServiceForm";
import { validateService } from "@/connections/egressServiceValidation";
import { githubRepositoryRules } from "@/connections/githubRepositoryRules";
import type {
  AuthoredEgressService,
  EgressCredentialService,
  EgressOAuthStatus,
  EgressValidation,
  EgressPreset,
  EgressRule,
  EgressRuleSummary,
} from "@/connections/types";

/**
 * What the egress tests share: the server's presets, the options of each context, helpers that run a service
 * through the form, the listing fixtures (services, rule summaries, account logins), and the helpers that wait for
 * an open editor's presets (`presetsReady`) and press a dialog button (`pressInDialog`).
 */

export const presets = egressPresets.presets as EgressPreset[];
// What the server sends with the presets; typing it checks the fixture has the shape the editor reads.
export const validation: EgressValidation = egressPresets.validation;
export const personal: EditorOptions = {
  context: "personal",
  sharedTarget: false,
  presets,
  validation,
};
export const sharedPersonal: EditorOptions = {
  ...personal,
  sharedTarget: true,
};
export const operator: EditorOptions = { ...personal, context: "operator" };
export const newService = {
  editing: false,
  takenNames: [] as string[],
  service: null,
};

export const bearerRule: EgressRule = {
  host: "api.example.com",
  auth: { type: "bearer" },
};

export const serialize = (
  service: AuthoredEgressService,
  options: EditorOptions = personal,
  name = "svc",
) => serializeService(formFromService(name, service), options, service);

export const problemsFor = (
  service: AuthoredEgressService | null,
  changes: Partial<EgressServiceForm> = {},
  options: EditorOptions = personal,
  settings: { editing: boolean; takenNames: string[] } = newService,
) =>
  validateService({ ...formFromService("svc", service), ...changes }, options, {
    ...settings,
    service,
  });

export const errorsFor = (
  service: AuthoredEgressService | null,
  changes: Partial<EgressServiceForm> = {},
  options: EditorOptions = personal,
  settings: { editing: boolean; takenNames: string[] } = newService,
) =>
  problemsFor(service, changes, options, settings).map(
    (problem) => problem.message,
  );

export function basicAuth(username: string) {
  return { type: "basic", username } as const;
}

export function validRule() {
  return { ...emptyRule(), host: "api.example.com" };
}

/** A limit's rules: they send nothing, so the server keeps them without auth, and they always cover uploads.github.com. */
export const limitRules = (repositories: string, graphql = false) =>
  githubRepositoryRules(repositories, { uploads: true, graphql }).rules.map(
    ({ auth: _auth, ...rule }) => rule,
  );

/** A limit to these repositories as the editor writes it and the server keeps it. */
export const storedLimit = (
  repositories: string,
  changes: Partial<AuthoredEgressService> = {},
): AuthoredEgressService => ({
  display_name: `GitHub (${repositories})`,
  description: `Limited to ${repositories}`,
  rules: limitRules(repositories),
  restrict_to_rules: true,
  restrict_only: true,
  ...changes,
});

/** Rules as a listing sums them up: host, port, and path prefix. */
export const ruleSummaries = (rules: EgressRule[]): EgressRuleSummary[] =>
  rules.map((rule) => ({
    host: rule.host,
    port: rule.port ?? null,
    path_prefix: rule.path_prefix ?? "/",
  }));

/** GitHub as the egress listings send it, with no key or account yet. */
export const credentialService = (
  changes: Partial<EgressCredentialService> = {},
): EgressCredentialService => ({
  name: "github",
  display_name: "GitHub",
  description: "GitHub API and git over HTTPS",
  source: "config",
  rules: [],
  restrict_only: false,
  is_shared: false,
  can_manage: true,
  configured: false,
  active_source: null,
  key_configured: false,
  key_updated_at: null,
  oauth: null,
  ...changes,
});

/** GitHub's account login as the listings send it: connectable, not connected. */
export const oauthStatus = (
  changes: Partial<EgressOAuthStatus> = {},
): EgressOAuthStatus => ({
  provider: "github",
  display_name: "GitHub",
  connected: false,
  account_label: null,
  can_connect: true,
  reset_required: false,
  service_account: false,
  unavailable_reason: null,
  shared_worker_opt_in: false,
  ...changes,
});

/** Wait until an open editor has the server's presets in its dropdown. */
export const presetsReady = () =>
  waitFor(() => expect(screen.getByLabelText("Preset")).toBeEnabled());

/** Press a button of the open dialog, such as its confirm or Cancel button. */
export async function pressInDialog(name: string) {
  fireEvent.click(
    within(await screen.findByRole("dialog")).getByRole("button", { name }),
  );
}
