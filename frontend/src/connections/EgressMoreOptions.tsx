import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { administratorLimitNote } from "./accountCopy";
import {
  operatorPlaceholderHint,
  personalPlaceholderHint,
} from "./egressPlaceholderRules";
import {
  BROKERED_PLACEHOLDER,
  type EditorContext,
  type EgressServiceForm,
  type PlaceholderForm,
  type RuleForm,
  emptyRule,
  usesOwnRules,
} from "./egressServiceForm";
import { configPlaceholderWarning } from "./egressServiceNotes";
import { placeholderField } from "./egressServiceValidation";
import {
  ExtraRule,
  Field,
  RulePathAndPortFields,
  type RuleEditing,
  errorClass,
  inputClass,
  selectClass,
  warningClass,
} from "./EgressRuleFields";
import type {
  EgressOAuthProviderOption,
  EgressPreset,
  EgressScope,
  EgressValidation,
} from "./types";

// What "Refuse other paths" does, in the order spec 12.4 gives it. The second
// sentence is about a user's own services, so the dashboard leaves it out. The
// restriction applies to everyone the store serves, so it says who that is.
const RESTRICT_HELP = {
  refusal:
    "Requests to these hosts whose path matches none of your rules are refused (path_not_allowed) instead of being sent without a key.",
  boundary:
    "The real boundary is the key itself, so scope it as narrowly as the website allows.",
  github:
    "GraphQL (/graphql) is refused unless listed, and a listed GraphQL rule reaches every repository the key can reach. Use a fine-grained token or a GitHub App installed on selected repositories.",
};

function restrictHelp(
  context: EditorContext,
  scope: EgressScope | undefined,
  onGithub: boolean,
): string {
  return [
    RESTRICT_HELP.refusal,
    // Without the scope there is no saying whom the limit reaches.
    context === "personal" && scope !== undefined
      ? administratorLimitNote(scope)
      : null,
    RESTRICT_HELP.boundary,
    onGithub ? RESTRICT_HELP.github : null,
  ]
    .filter((sentence) => sentence !== null)
    .join(" ");
}

/**
 * The first rule's path and port, every other rule, and Add rule; the first
 * rule's host is in the basic form. A limit's rules send no key, so they have
 * no way to send one.
 */
export function RulesSection({
  id,
  form,
  set,
  problemAt,
  disabled,
  context,
  validation,
  ruleEditing,
  sendsKey = true,
}: {
  /** The id of the section's heading. */
  id: string;
  form: EgressServiceForm;
  set: (changes: Partial<EgressServiceForm>) => void;
  problemAt: (field: string) => string | undefined;
  disabled: boolean;
  context: EditorContext;
  /** What the server checks on save, for the most rules; `null` until it says. */
  validation: EgressValidation | null;
  ruleEditing: (index: number, rule: RuleForm) => RuleEditing;
  sendsKey?: boolean;
}) {
  const rulesProblem = problemAt("rules");
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h4 id={id} className="text-sm font-medium">
        Paths and extra rules
      </h4>
      <div className="grid gap-3 sm:grid-cols-2">
        <RulePathAndPortFields
          {...ruleEditing(0, form.rules[0] ?? emptyRule())}
        />
      </div>
      <ul className="space-y-3">
        {form.rules.slice(1).map((rule, offset) => (
          <ExtraRule
            // Rules have no identity beyond their position.
            key={offset + 1}
            {...ruleEditing(offset + 1, rule)}
            sendsKey={sendsKey}
            onRemove={() =>
              set({
                rules: form.rules.filter((_, i) => i !== offset + 1),
              })
            }
          />
        ))}
      </ul>
      {rulesProblem && (
        // No field holds this problem, so the message takes the focus.
        <p tabIndex={-1} data-problem-focus="" className={errorClass}>
          {rulesProblem}
        </p>
      )}
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={
          disabled ||
          (context === "personal" &&
            validation !== null &&
            form.rules.length >= validation.max_rules_per_service)
        }
        onClick={() => set({ rules: [...form.rules, emptyRule()] })}
      >
        Add rule
      </Button>
    </section>
  );
}

/**
 * Everything the basic form leaves out: display name and description, the
 * preset's rules, paths and extra rules, the restriction, the account login,
 * and the placeholders.
 */
export function MoreOptions({
  id,
  form,
  set,
  problemAt,
  disabled,
  context,
  scope,
  sharedTarget,
  validation,
  preset,
  providers,
  onGithub,
  configPlaceholders,
  ruleEditing,
  newService,
}: {
  /** The id of the section, which the toggle controls. */
  id: string;
  form: EgressServiceForm;
  set: (changes: Partial<EgressServiceForm>) => void;
  problemAt: (field: string) => string | undefined;
  disabled: boolean;
  context: EditorContext;
  /** Whose store the service lives in, for whom its restriction also limits the administrator's services. */
  scope: EgressScope | undefined;
  /** Shared and unscoped agents cannot use personal accounts. */
  sharedTarget: boolean;
  /** What the server checks on save, for the most rules and the placeholder hint; `null` until it says. */
  validation: EgressValidation | null;
  preset: EgressPreset | undefined;
  /** The account logins a custom service can use. */
  providers: readonly EgressOAuthProviderOption[];
  /** The service is on GitHub, so the help adds what GraphQL does. */
  onGithub: boolean;
  /** Variable name to the display name of the administrator's service that sets it. */
  configPlaceholders: Readonly<Record<string, string>>;
  ruleEditing: (index: number, rule: RuleForm) => RuleEditing;
  /** The service is being added, so an empty display name is saved as its host; an edited one keeps none. */
  newService: boolean;
}) {
  const primaryHost = (form.rules[0]?.host ?? "").trim().toLowerCase();
  const displayNameHint = preset
    ? `Leave empty to use ${preset.display_name}.`
    : primaryHost && newService
      ? `Leave empty to use ${primaryHost}.`
      : undefined;
  // The server's placeholder rules the hint spells out; `null` until they load or when they could not be loaded.
  const placeholderRules = validation?.placeholder ?? null;
  const setPlaceholder = (index: number, placeholder: PlaceholderForm) =>
    set({
      placeholders: form.placeholders.map((existing, i) =>
        i === index ? placeholder : existing,
      ),
    });
  const loginField = preset?.oauth_provider ? (
    <p className="text-xs text-muted-foreground">
      {sharedTarget
        ? "Accounts cannot be connected on a shared agent, so this service uses an API key."
        : `Users can connect an account (${preset.oauth_provider}) instead of an API key.`}
    </p>
  ) : (
    !preset && (
      <Field
        label="Account login (optional)"
        id={`${id}-oauth`}
        hint={
          sharedTarget
            ? "Accounts cannot be connected on a shared agent. Use an API key."
            : "Users can connect an account of this kind instead of an API key. Leave it empty to use an API key."
        }
        error={problemAt("oauthProvider")}
      >
        {(control) => (
          <select
            {...control}
            className={`${selectClass} max-w-xs`}
            value={sharedTarget ? "" : form.oauthProvider}
            disabled={sharedTarget || disabled}
            onChange={(event) => set({ oauthProvider: event.target.value })}
          >
            <option value="">No account login</option>
            {providers.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.display_name}
              </option>
            ))}
            {/* A login the service already has that the server does not list stays selectable. */}
            {form.oauthProvider !== "" &&
              !providers.some(
                (provider) => provider.id === form.oauthProvider,
              ) && (
                <option value={form.oauthProvider}>{form.oauthProvider}</option>
              )}
          </select>
        )}
      </Field>
    )
  );

  return (
    <div id={id} className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Display name" id={`${id}-display`} hint={displayNameHint}>
          {(control) => (
            <Input
              {...control}
              className={inputClass}
              value={form.displayName}
              disabled={disabled}
              onChange={(event) => set({ displayName: event.target.value })}
            />
          )}
        </Field>
        <Field label="Description" id={`${id}-description`}>
          {(control) => (
            <Input
              {...control}
              className={inputClass}
              value={form.description}
              disabled={disabled}
              onChange={(event) => set({ description: event.target.value })}
            />
          )}
        </Field>
      </div>

      {preset && (
        <label className="flex items-start gap-2 text-xs">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4"
            checked={form.replacePresetRules}
            disabled={disabled}
            onChange={(event) =>
              set({
                replacePresetRules: event.target.checked,
                rules:
                  event.target.checked && form.rules.length === 0
                    ? [emptyRule()]
                    : form.rules,
              })
            }
          />
          <span>Replace the preset's rules with my own</span>
        </label>
      )}

      {usesOwnRules(form) && (
        <RulesSection
          id={`${id}-rules`}
          form={form}
          set={set}
          problemAt={problemAt}
          disabled={disabled}
          context={context}
          validation={validation}
          ruleEditing={ruleEditing}
        />
      )}

      <div className="space-y-1">
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4"
            checked={form.restrictToRules}
            disabled={disabled}
            onChange={(event) => set({ restrictToRules: event.target.checked })}
          />
          <span className="font-medium">Refuse other paths on these hosts</span>
        </label>
        <p className="pl-6 text-xs text-muted-foreground">
          {restrictHelp(context, scope, onGithub)}
        </p>
      </div>

      {loginField}

      {/* The placeholder variables: what they are for, the rules for their names, one row each with its problem or warning. */}
      <div className="space-y-3">
        <div className="space-y-1">
          <h4 className="text-sm font-medium">Placeholder variables</h4>
          <p className="text-xs text-muted-foreground">
            Environment variables set in the agent's sandbox so tools find a key
            to send. Use a fixed value such as {BROKERED_PLACEHOLDER}; the
            broker swaps in the real key. Placeholders are set only while this
            service has a key or a connected account.
            {preset
              ? form.explicitlyEmpty.placeholders
                ? ` This service sets none, which drops the placeholders of the ${preset.display_name} preset.`
                : ` Leave the list empty to use the placeholders of the ${preset.display_name} preset.`
              : ""}
          </p>
          <p className="text-xs text-muted-foreground">
            {context === "personal"
              ? personalPlaceholderHint(placeholderRules)
              : operatorPlaceholderHint(placeholderRules)}
          </p>
        </div>
        {form.placeholders.map((placeholder, index) => {
          const nameError = problemAt(placeholderField(index, "name"));
          const valueError = problemAt(placeholderField(index, "value"));
          const warning = nameError
            ? null
            : configPlaceholderWarning(placeholder.name, configPlaceholders);
          const nameNoteId = `${id}-placeholder-${index}-name-note`;
          const valueNoteId = `${id}-placeholder-${index}-value-note`;
          return (
            <div key={index} className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label={`Placeholder ${index + 1} name`}
                  aria-invalid={nameError ? true : undefined}
                  aria-describedby={
                    nameError || warning ? nameNoteId : undefined
                  }
                  placeholder="MY_API_KEY"
                  className="h-9 w-56 font-mono text-xs aria-invalid:border-destructive"
                  value={placeholder.name}
                  disabled={disabled}
                  onChange={(event) =>
                    setPlaceholder(index, {
                      ...placeholder,
                      name: event.target.value,
                    })
                  }
                />
                <Input
                  aria-label={`Placeholder ${index + 1} value`}
                  aria-invalid={valueError ? true : undefined}
                  aria-describedby={valueError ? valueNoteId : undefined}
                  className="h-9 w-56 font-mono text-xs aria-invalid:border-destructive"
                  value={placeholder.value}
                  disabled={disabled}
                  onChange={(event) =>
                    setPlaceholder(index, {
                      ...placeholder,
                      value: event.target.value,
                    })
                  }
                />
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove placeholder ${index + 1}`}
                  disabled={disabled}
                  onClick={() =>
                    set({
                      placeholders: form.placeholders.filter(
                        (_, i) => i !== index,
                      ),
                    })
                  }
                >
                  Remove
                </Button>
              </div>
              {nameError && (
                <p id={nameNoteId} className={errorClass}>
                  {nameError}
                </p>
              )}
              {warning && (
                <p id={nameNoteId} className={warningClass}>
                  {warning}
                </p>
              )}
              {valueError && (
                <p id={valueNoteId} className={errorClass}>
                  {valueError}
                </p>
              )}
            </div>
          );
        })}
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={() =>
            set({
              placeholders: [
                ...form.placeholders,
                { name: "", value: BROKERED_PLACEHOLDER },
              ],
            })
          }
        >
          Add placeholder
        </Button>
      </div>
    </div>
  );
}
