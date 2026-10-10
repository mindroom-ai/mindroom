import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  BROKERED_PLACEHOLDER,
  DEFAULT_TEMPLATE,
  type EditorContext,
  type EditorOptions,
  type EgressServiceForm,
  type PlaceholderForm,
  type RuleForm,
  type ServiceCoverage,
  MAX_RULES,
  emptyRule,
  findBroaderServices,
  formFromService,
  formRuleSummaries,
  githubRepositoryRules,
  looksLikeGithub,
  presetOf,
  ruleFormFrom,
  serializeService,
  usesOwnRules,
  validateService,
} from "./egressServiceModel";
import type {
  AuthoredEgressService,
  EgressAuthType,
  EgressPreset,
  EgressRule,
  EgressRuleSummary,
} from "./types";

const AUTH_TYPES: { value: EgressAuthType; label: string }[] = [
  { value: "bearer", label: "Bearer token" },
  { value: "basic", label: "Basic (username and key)" },
  { value: "header", label: "Header" },
  { value: "query", label: "Query parameter" },
];

/** A rule as a short host and path, such as `www.googleapis.com/drive/`. */
function ruleCoverage(rule: EgressRuleSummary): string {
  const path = rule.path_prefix === "/" ? "" : rule.path_prefix;
  return `${rule.host}${rule.port === null ? "" : `:${rule.port}`}${path}`;
}

const selectClass =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm";

function Field({
  label,
  htmlFor,
  hint,
  children,
  className,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`space-y-1 ${className ?? ""}`}>
      <label htmlFor={htmlFor} className="text-xs font-medium">
        {label}
      </label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function RuleFields({
  index,
  rule,
  onChange,
  onRemove,
}: {
  index: number;
  rule: RuleForm;
  onChange: (rule: RuleForm) => void;
  onRemove: () => void;
}) {
  const id = useId();
  const label = `Rule ${index + 1}`;
  const set = (changes: Partial<RuleForm>) => onChange({ ...rule, ...changes });
  const needsTemplate = rule.authType === "header" || rule.authType === "query";
  return (
    <li
      aria-label={label}
      className="space-y-3 rounded-md border border-border/60 p-3"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium">{label}</span>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          aria-label={`Remove ${label}`}
          onClick={onRemove}
        >
          Remove
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Host" htmlFor={`${id}-host`}>
          <Input
            id={`${id}-host`}
            aria-label={`${label} host`}
            placeholder="api.example.com or *.example.com"
            className="h-9"
            value={rule.host}
            onChange={(event) => set({ host: event.target.value })}
          />
        </Field>
        <Field label="Path prefix" htmlFor={`${id}-path`}>
          <Input
            id={`${id}-path`}
            aria-label={`${label} path prefix`}
            placeholder="/ (every path)"
            className="h-9"
            value={rule.pathPrefix}
            onChange={(event) => set({ pathPrefix: event.target.value })}
          />
        </Field>
        <Field label="Port" htmlFor={`${id}-port`}>
          <Input
            id={`${id}-port`}
            aria-label={`${label} port`}
            inputMode="numeric"
            placeholder="any"
            className="h-9"
            value={rule.port}
            onChange={(event) => set({ port: event.target.value })}
          />
        </Field>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Adds the key as" htmlFor={`${id}-auth`}>
          <select
            id={`${id}-auth`}
            aria-label={`${label} auth type`}
            className={selectClass}
            value={rule.authType}
            onChange={(event) =>
              set({ authType: event.target.value as EgressAuthType })
            }
          >
            {AUTH_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
        </Field>
        {rule.authType === "basic" && (
          <Field label="Username" htmlFor={`${id}-username`}>
            <Input
              id={`${id}-username`}
              aria-label={`${label} username`}
              className="h-9"
              value={rule.username}
              onChange={(event) => set({ username: event.target.value })}
            />
          </Field>
        )}
        {needsTemplate && (
          <>
            <Field
              label={
                rule.authType === "header" ? "Header name" : "Parameter name"
              }
              htmlFor={`${id}-name`}
            >
              <Input
                id={`${id}-name`}
                aria-label={`${label} ${rule.authType === "header" ? "header name" : "parameter name"}`}
                placeholder={
                  rule.authType === "header" ? "x-api-key" : "api_key"
                }
                className="h-9"
                value={rule.headerOrParam}
                onChange={(event) => set({ headerOrParam: event.target.value })}
              />
            </Field>
            <Field label="Value template" htmlFor={`${id}-template`}>
              <Input
                id={`${id}-template`}
                aria-label={`${label} template`}
                placeholder={DEFAULT_TEMPLATE}
                className="h-9"
                value={rule.template}
                onChange={(event) => set({ template: event.target.value })}
              />
            </Field>
          </>
        )}
      </div>
    </li>
  );
}

function GithubHelper({ onApply }: { onApply: (rules: EgressRule[]) => void }) {
  const id = useId();
  const [repositories, setRepositories] = useState("");
  const [uploads, setUploads] = useState(false);
  const [graphql, setGraphql] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  const apply = () => {
    const result = githubRepositoryRules(repositories, { uploads, graphql });
    setErrors(result.errors);
    if (result.errors.length === 0) onApply(result.rules);
  };

  return (
    <section
      aria-labelledby={`${id}-heading`}
      className="space-y-3 rounded-md border border-border/60 bg-muted/20 p-3"
    >
      <div className="space-y-1">
        <h4 id={`${id}-heading`} className="text-sm font-medium">
          Limit to repositories
        </h4>
        <p className="text-xs text-muted-foreground">
          One repository per line, as owner/repo, or owner/* for everything an
          owner has. Applying replaces the rules above with rules for just those
          repositories (API and git over HTTPS) and refuses every other path on
          GitHub. Other API paths, such as /user or search, stop working.
          Matching is case sensitive, so write names as they appear in GitHub
          URLs.
        </p>
      </div>
      <Textarea
        aria-label="GitHub repositories"
        placeholder={"basnijholt/agent-cli\nmindroom-ai/*"}
        rows={3}
        className="font-mono text-xs"
        value={repositories}
        onChange={(event) => setRepositories(event.target.value)}
      />
      <label className="flex items-start gap-2 text-xs">
        <input
          type="checkbox"
          className="mt-0.5 h-4 w-4"
          checked={uploads}
          onChange={(event) => setUploads(event.target.checked)}
        />
        <span>
          Also allow uploads.github.com for these repositories (release assets)
        </span>
      </label>
      <label className="flex items-start gap-2 text-xs">
        <input
          type="checkbox"
          className="mt-0.5 h-4 w-4"
          checked={graphql}
          onChange={(event) => setGraphql(event.target.checked)}
        />
        <span>
          Also allow GraphQL (/graphql). GraphQL reaches every repository the
          key can reach, not just the ones listed.
        </span>
      </label>
      {errors.length > 0 && (
        <Alert variant="destructive" className="p-2">
          <AlertDescription>
            <ul className="list-disc pl-4">
              {errors.map((error, index) => (
                <li key={`${index}-${error}`}>{error}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      <Button type="button" size="sm" variant="outline" onClick={apply}>
        Use these repositories
      </Button>
    </section>
  );
}

export interface EgressServiceEditorProps {
  /** The service as authored, or `null` to add one. */
  service: AuthoredEgressService | null;
  /** The name of the service being edited; it cannot change. Omit it to add one. */
  name?: string;
  context: EditorContext;
  /** Shared and unscoped agents cannot use personal accounts. */
  sharedTarget?: boolean;
  /** Names a new service cannot take. */
  takenNames?: readonly string[];
  /**
   * The other services of the same scope, with where they apply. The form warns
   * when one already covers a rule of this service more widely, since
   * restricting paths cannot narrow that service. The service being edited is
   * left out by name.
   */
  otherServices?: readonly ServiceCoverage[];
  /** Fetch the server's presets when the form opens. */
  loadPresets: (signal: AbortSignal) => Promise<EgressPreset[]>;
  /**
   * Said before a save when saving has a wider effect, such as writing other
   * unsaved settings too; the user confirms once before the save goes out.
   */
  saveWarning?: string | null;
  /** Called when the form starts or stops differing from what it opened with. */
  onDirtyChange?: (dirty: boolean) => void;
  /** Save the service; reject with an Error whose message is shown in the form. */
  onSave: (name: string, service: AuthoredEgressService) => Promise<void>;
  /** Delete the service being edited; reject like `onSave`. Without it there is no delete button. */
  onDelete?: () => Promise<void>;
  onCancel: () => void;
}

/**
 * Form for one egress service: its preset or rules, placeholders, and limits.
 *
 * Used on the personal Connections page (the caller's own services) and on the
 * dashboard (services in config.yaml). It always hands back the authored form.
 */
export function EgressServiceEditor({
  service,
  name,
  context,
  sharedTarget = false,
  takenNames = [],
  otherServices = [],
  loadPresets,
  saveWarning = null,
  onDirtyChange,
  onSave,
  onDelete,
  onCancel,
}: EgressServiceEditorProps) {
  const id = useId();
  const editing = name !== undefined;
  // `null` while the presets load, then the server's list (empty when it could not be loaded).
  const [presets, setPresets] = useState<EgressPreset[] | null>(null);
  const [presetsFailed, setPresetsFailed] = useState(false);
  const options: EditorOptions = { context, sharedTarget, presets };
  // Checked against the names the editor opened with: a save that is still
  // running may already have written this name into the page's own list.
  const [openedWithNames] = useState(takenNames);
  const [form, setForm] = useState(() => formFromService(name ?? "", service));
  const [advancedOpen, setAdvancedOpen] = useState(
    form.placeholders.length > 0,
  );
  const [attempted, setAttempted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | "delete" | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingSave, setConfirmingSave] = useState(false);
  const loadPresetsRef = useRef(loadPresets);
  const openedWith = useRef(form);
  const dirty = useMemo(
    () => JSON.stringify(form) !== JSON.stringify(openedWith.current),
    [form],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadPresetsRef
      .current(controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) setPresets(loaded);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setPresets([]);
        setPresetsFailed(true);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const set = (changes: Partial<EgressServiceForm>) => {
    setConfirmingSave(false);
    setForm((previous) => ({ ...previous, ...changes }));
  };
  const setRule = (index: number, rule: RuleForm) =>
    set({
      rules: form.rules.map((existing, i) => (i === index ? rule : existing)),
    });
  const setPlaceholder = (index: number, placeholder: PlaceholderForm) =>
    set({
      placeholders: form.placeholders.map((existing, i) =>
        i === index ? placeholder : existing,
      ),
    });

  const preset = presetOf(presets, form.preset);
  const settings = { editing, takenNames: openedWithNames };
  const validation = attempted ? validateService(form, options, settings) : [];
  const shownErrors =
    validation.length > 0 ? validation : serverError ? [serverError] : [];
  const title = editing ? `Edit ${name}` : "Add service";
  const isPersonal = context === "personal";
  const showGithubHelper = looksLikeGithub(form);
  const broaderServices = form.restrictToRules
    ? findBroaderServices(
        formRuleSummaries(form, presets),
        otherServices.filter((other) => other.name !== name),
      )
    : [];

  const choosePreset = (value: string) =>
    set({
      preset: value,
      replacePresetRules: false,
      rules: form.rules.length > 0 ? form.rules : [emptyRule()],
    });

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setAttempted(true);
    setServerError(null);
    if (validateService(form, options, settings).length > 0) return;
    if (saveWarning && !confirmingSave) {
      setConfirmingSave(true);
      return;
    }
    setConfirmingSave(false);
    setBusy("save");
    try {
      await onSave(form.name, serializeService(form, options, service));
    } catch (cause) {
      setServerError(
        cause instanceof Error && cause.message
          ? cause.message
          : "Could not save the service. Try again.",
      );
      setBusy(null);
    }
  };

  const remove = async () => {
    if (!onDelete) return;
    setBusy("delete");
    setServerError(null);
    try {
      await onDelete();
    } catch (cause) {
      setServerError(
        cause instanceof Error && cause.message
          ? cause.message
          : "Could not delete the service. Try again.",
      );
      setBusy(null);
      setConfirmingDelete(false);
    }
  };

  return (
    <form
      aria-label={title}
      noValidate
      className="space-y-5 bg-muted/20 px-5 py-4"
      onSubmit={(event) => void submit(event)}
    >
      <h3 className="font-semibold">{title}</h3>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="Name"
          htmlFor={`${id}-name`}
          hint={
            editing
              ? "The name cannot be changed."
              : "Lowercase letters, digits, - and _. It cannot be changed later."
          }
        >
          <Input
            id={`${id}-name`}
            className="h-9"
            value={form.name}
            disabled={editing || busy !== null}
            onChange={(event) => set({ name: event.target.value })}
          />
        </Field>
        <Field label="Preset" htmlFor={`${id}-preset`}>
          <select
            id={`${id}-preset`}
            className={selectClass}
            value={form.preset}
            disabled={busy !== null || presets === null}
            onChange={(event) => choosePreset(event.target.value)}
          >
            <option value="">{presets === null ? "Loading…" : "Custom"}</option>
            {(presets ?? []).map((option) => (
              <option key={option.id} value={option.id}>
                {option.display_name}
              </option>
            ))}
            {form.preset !== "" && !preset && (
              <option value={form.preset}>{form.preset}</option>
            )}
          </select>
          {presetsFailed && (
            <p className="text-xs text-muted-foreground">
              The preset list could not be loaded. You can still use a custom
              service.
            </p>
          )}
        </Field>
        <Field
          label="Display name"
          htmlFor={`${id}-display`}
          hint={
            preset ? `Leave empty to use ${preset.display_name}.` : undefined
          }
        >
          <Input
            id={`${id}-display`}
            className="h-9"
            value={form.displayName}
            disabled={busy !== null}
            onChange={(event) => set({ displayName: event.target.value })}
          />
        </Field>
        <Field label="Description" htmlFor={`${id}-description`}>
          <Input
            id={`${id}-description`}
            className="h-9"
            value={form.description}
            disabled={busy !== null}
            onChange={(event) => set({ description: event.target.value })}
          />
        </Field>
      </div>

      <section aria-labelledby={`${id}-rules`} className="space-y-3">
        <h4 id={`${id}-rules`} className="text-sm font-medium">
          Rules
        </h4>
        {preset && !form.replacePresetRules && (
          <p className="text-xs text-muted-foreground">
            The {preset.display_name} preset covers{" "}
            {preset.rules.map(ruleCoverage).join(", ")}.
          </p>
        )}
        {preset && (
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4"
              checked={form.replacePresetRules}
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
          <>
            <ul className="space-y-3">
              {form.rules.map((rule, index) => (
                <RuleFields
                  // Rules have no identity beyond their position.
                  key={index}
                  index={index}
                  rule={rule}
                  onChange={(next) => setRule(index, next)}
                  onRemove={() =>
                    set({ rules: form.rules.filter((_, i) => i !== index) })
                  }
                />
              ))}
            </ul>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={isPersonal && form.rules.length >= MAX_RULES}
              onClick={() => set({ rules: [...form.rules, emptyRule()] })}
            >
              Add rule
            </Button>
          </>
        )}
        {showGithubHelper && (
          <GithubHelper
            onApply={(rules) =>
              set({
                rules: rules.map(ruleFormFrom),
                replacePresetRules: form.preset !== "",
                restrictToRules: true,
              })
            }
          />
        )}
      </section>

      <div className="space-y-1">
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4"
            checked={form.restrictToRules}
            onChange={(event) => set({ restrictToRules: event.target.checked })}
          />
          <span className="font-medium">Refuse other paths on these hosts</span>
        </label>
        <p className="pl-6 text-xs text-muted-foreground">
          Requests to this service's hosts whose path matches no rule are
          refused with path_not_allowed instead of being sent without
          credentials. GraphQL (/graphql) is refused too unless a rule lists it,
          and a listed GraphQL rule reaches every repository the key can reach.
          The real permission boundary is the key itself: use a fine-grained
          GitHub token, or a GitHub App installed on selected repositories.
        </p>
        {broaderServices.length > 0 && (
          // A standing notice, not something to announce on every render.
          <Alert role="note" className="ml-6 w-auto p-2">
            <AlertDescription>
              A broader rule of {broaderServices.join(" and ")} on the same host
              already covers some of these paths. Refusing other paths cannot
              narrow it: requests that match that rule are still sent with that
              service's credentials.
            </AlertDescription>
          </Alert>
        )}
      </div>

      {preset?.oauth_provider ? (
        <p className="text-xs text-muted-foreground">
          {sharedTarget
            ? "Accounts cannot be connected on a shared agent, so this service uses an API key."
            : `Users can connect an account (${preset.oauth_provider}) instead of an API key.`}
        </p>
      ) : (
        !preset && (
          <Field
            label="Account login (optional)"
            htmlFor={`${id}-oauth`}
            hint={
              sharedTarget
                ? "Accounts cannot be connected on a shared agent. Use an API key."
                : "The id of an OAuth provider MindRoom knows, such as github, google_drive, or atlassian. Leave empty to use an API key."
            }
          >
            <Input
              id={`${id}-oauth`}
              className="h-9 max-w-xs"
              value={sharedTarget ? "" : form.oauthProvider}
              disabled={sharedTarget || busy !== null}
              onChange={(event) => set({ oauthProvider: event.target.value })}
            />
          </Field>
        )
      )}

      <section className="space-y-2">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          aria-expanded={advancedOpen}
          aria-controls={`${id}-advanced`}
          onClick={() => setAdvancedOpen((open) => !open)}
        >
          Advanced
        </Button>
        {advancedOpen && (
          <div id={`${id}-advanced`} className="space-y-3">
            <div className="space-y-1">
              <h4 className="text-sm font-medium">Placeholder variables</h4>
              <p className="text-xs text-muted-foreground">
                Environment variables set in the agent's sandbox so tools find a
                key to send. Use a fixed value such as {BROKERED_PLACEHOLDER};
                the broker swaps in the real key.
                {preset
                  ? form.explicitlyEmpty.placeholders
                    ? ` This service sets none, which drops the placeholders of the ${preset.display_name} preset.`
                    : ` Leave the list empty to use the placeholders of the ${preset.display_name} preset.`
                  : ""}
              </p>
              <p className="text-xs text-muted-foreground">
                {isPersonal
                  ? "Names use capital letters, digits, and underscores, and one word between underscores must be TOKEN, KEY, SECRET, PASSWORD, PAT, AUTH, or CREDENTIAL (for example MY_API_KEY). Values are 1 to 256 letters, digits, or . _ : -"
                  : "Names use capital letters, digits, and underscores. PATH, HOME, proxy and CA variables, and names starting with MINDROOM_ or GIT_CONFIG_ are reserved."}
              </p>
            </div>
            {form.placeholders.map((placeholder, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label={`Placeholder ${index + 1} name`}
                  placeholder="MY_API_KEY"
                  className="h-9 w-56 font-mono text-xs"
                  value={placeholder.name}
                  onChange={(event) =>
                    setPlaceholder(index, {
                      ...placeholder,
                      name: event.target.value,
                    })
                  }
                />
                <Input
                  aria-label={`Placeholder ${index + 1} value`}
                  className="h-9 w-56 font-mono text-xs"
                  value={placeholder.value}
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
            ))}
            <Button
              type="button"
              size="sm"
              variant="outline"
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
        )}
      </section>

      {shownErrors.length > 0 && (
        <Alert variant="destructive" className="p-2">
          <AlertDescription>
            {shownErrors.length === 1 ? (
              shownErrors[0]
            ) : (
              <ul className="list-disc pl-4">
                {shownErrors.map((error, index) => (
                  <li key={`${index}-${error}`}>{error}</li>
                ))}
              </ul>
            )}
          </AlertDescription>
        </Alert>
      )}

      {confirmingSave && saveWarning && (
        <Alert className="p-2">
          <AlertDescription>{saveWarning}</AlertDescription>
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="submit"
          size="sm"
          disabled={busy !== null || presets === null}
        >
          {busy === "save" ? (
            <>
              <Loader2
                className="mr-2 h-3.5 w-3.5 animate-spin"
                aria-hidden="true"
              />
              Saving…
            </>
          ) : confirmingSave ? (
            "Save all changes"
          ) : (
            "Save service"
          )}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy !== null}
          onClick={confirmingSave ? () => setConfirmingSave(false) : onCancel}
        >
          {confirmingSave ? "Go back" : "Cancel"}
        </Button>
        {editing && onDelete && !confirmingDelete && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={busy !== null}
            onClick={() => setConfirmingDelete(true)}
          >
            Delete service
          </Button>
        )}
        {editing && onDelete && confirmingDelete && (
          <span className="ml-auto flex flex-wrap items-center gap-2 text-xs">
            Delete {name}?{saveWarning ? ` ${saveWarning}` : ""}
            <Button
              type="button"
              size="sm"
              variant="destructive"
              disabled={busy !== null}
              onClick={() => void remove()}
            >
              {busy === "delete" ? "Deleting…" : "Confirm delete"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy !== null}
              onClick={() => setConfirmingDelete(false)}
            >
              Keep
            </Button>
          </span>
        )}
      </div>
    </form>
  );
}

/** Asks before the open editor's unsaved changes are thrown away for another action. */
export function DiscardEditsNotice({
  onDiscard,
  onKeep,
}: {
  onDiscard: () => void;
  onKeep: () => void;
}) {
  return (
    <Alert className="m-4 w-auto p-2">
      <AlertDescription className="flex flex-wrap items-center gap-2">
        You have unsaved changes in the open editor.
        <Button size="sm" variant="destructive" onClick={onDiscard}>
          Discard changes
        </Button>
        <Button size="sm" variant="ghost" onClick={onKeep}>
          Keep editing
        </Button>
      </AlertDescription>
    </Alert>
  );
}
