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

/** GitHub's account login as the listings send it: connectable, not connected, and allowed for Agent Keys. */
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
  reuse_enabled: true,
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

/** Opens a row's ⋯ menu from its button, closing any other menu first, and returns the open menu. */
async function openMenu(trigger: HTMLElement) {
  for (const menu of screen.queryAllByRole("menu"))
    if (menu.getAttribute("aria-labelledby") !== trigger.id)
      fireEvent.keyDown(menu, { key: "Escape" });
  if (trigger.getAttribute("aria-expanded") !== "true")
    fireEvent.keyDown(trigger, { key: "Enter" });
  return within(
    await screen.findByRole("menu", {
      name: trigger.getAttribute("aria-label") ?? "",
    }),
  );
}

const closeMenus = () => {
  for (const menu of screen.queryAllByRole("menu"))
    fireEvent.keyDown(menu, { key: "Escape" });
};

/**
 * The item of a row's ⋯ menu that an action such as "Edit service for GitHub"
 * names, with that menu open once the row is shown. `scope` picks the row
 * when several tables have one with that name.
 */
export async function rowAction(
  action: string,
  scope: HTMLElement = document.body,
) {
  const service = action.slice(action.lastIndexOf(" for ") + " for ".length);
  const menu = await openMenu(
    await within(scope).findByRole("button", {
      name: `More actions for ${service}`,
    }),
  );
  return menu.getByRole("menuitem", { name: action });
}

/**
 * A row's actions in order, its buttons and then its ⋯ menu items, each with
 * its visible text and accessible name.
 */
export async function rowControls(
  service: string,
  scope: HTMLElement = document.body,
) {
  const row = within(scope).getByRole("row", { name: service });
  const control = (element: HTMLElement) => ({
    text: element.textContent ?? "",
    name: element.getAttribute("aria-label") ?? element.textContent ?? "",
  });
  const trigger = within(row).queryByRole("button", {
    name: `More actions for ${service}`,
  });
  const controls = within(row)
    .queryAllByRole("button")
    .filter((button) => button !== trigger)
    .map(control);
  if (trigger) {
    controls.push(
      ...(await openMenu(trigger)).getAllByRole("menuitem").map(control),
    );
    closeMenus();
  }
  return controls;
}

/** Asserts that no button in `scope`, and no item of any ⋯ menu there, matches `name`. */
export async function expectNoAction(
  name: RegExp | string,
  scope: HTMLElement = document.body,
) {
  expect(within(scope).queryByRole("button", { name })).toBeNull();
  const matches = (label: string) =>
    typeof name === "string" ? label === name : name.test(label);
  for (const trigger of within(scope).queryAllByRole("button", {
    name: /^More actions for /,
  })) {
    const names = (await openMenu(trigger))
      .getAllByRole("menuitem")
      .map((item) => item.getAttribute("aria-label") ?? "");
    closeMenus();
    expect(names.filter(matches)).toEqual([]);
  }
}

/** Matches the element whose text, split over several elements such as code, reads `text`. */
export const wholeText =
  (text: string) => (_content: string, element: Element | null) =>
    element?.textContent === text &&
    ![...element.children].some((child) => child.textContent === text);
