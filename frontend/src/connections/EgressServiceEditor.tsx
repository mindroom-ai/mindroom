import {
  type FormEvent,
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
import {
  administratorLimitNote,
  deleteServiceWarning,
  removeLimitWarning,
} from "./accountCopy";
import {
  type EditorContext,
  type EditorOptions,
  type EgressServiceForm,
  type RuleForm,
  NOT_EXPLICITLY_EMPTY,
  emptyRule,
  formFromService,
  presetOf,
  serializeService,
  suggestedName,
  usesMoreOptions,
  usesOwnRules,
} from "./egressServiceForm";
import {
  overlapWarnings,
  presetPlaceholderWarnings,
  scopeNote,
} from "./egressServiceNotes";
import {
  type RuleFieldPart,
  type ServiceProblem,
  isBasicField,
  ruleField,
  validateService,
} from "./egressServiceValidation";
import { hostPort, limitOverlap } from "./egressRowText";
import { looksLikeGithub } from "./githubLimits";
import {
  GITHUB_API_HOST,
  GITHUB_UPLOADS_HOST,
  GITHUB_WEB_HOST,
  githubRepositoryRules,
} from "./githubRepositoryRules";
import {
  GithubHelper,
  githubHelperChanges,
  helperSignature,
  restoredHelperInput,
} from "./EgressGithubHelper";
import type { HelperInput, HelperLabels } from "./EgressGithubHelper";
import { MoreOptions, RulesSection } from "./EgressMoreOptions";
import { errorText } from "./request";
import {
  Field,
  RuleHostField,
  RuleSendFields,
  type RuleEditing,
  inputClass,
  selectClass,
  warningClass,
} from "./EgressRuleFields";
import type {
  AuthoredEgressService,
  EgressCredentialService,
  EgressValidation,
  EgressOAuthProviderOption,
  EgressPreset,
  EgressPresetCatalog,
  EgressRuleSummary,
  EgressScope,
} from "./types";

/** A problem with no field to show it, or with a field the basic form does not have. */
function needsMoreOptions(problem: ServiceProblem): boolean {
  return problem.field !== "form" && !isBasicField(problem.field);
}

/** The name a new limit starts with: `github-limited`, or the first of `-2`, `-3`... that is free. */
function limitedServiceName(taken: readonly string[]): string {
  const base = "github-limited";
  for (let n = 1; ; n += 1) {
    const name = n === 1 ? base : `${base}-${n}`;
    if (!taken.includes(name)) return name;
  }
}

/** A rule as a short host and path, such as `www.googleapis.com/drive/`. */
function ruleCoverage(rule: EgressRuleSummary): string {
  const path = rule.path_prefix === "/" ? "" : rule.path_prefix;
  return `${hostPort(rule)}${path}`;
}

export interface EgressServiceEditorProps {
  /** The service as authored, or `null` to add one. */
  service: AuthoredEgressService | null;
  /** The name of the service being edited; it cannot change. Omit it to add one. */
  name?: string;
  /**
   * Add mode only: add a service that only limits GitHub repositories, which
   * has no key or account of its own, named after the first free
   * `github-limited` name. Editing a service that is restrict-only opens the
   * same form, with the repository list when that list writes back exactly
   * the limit's rules, and with the rule fields otherwise.
   */
  initialRestrictOnly?: boolean;
  context: EditorContext;
  /** Shared and unscoped agents cannot use personal accounts. */
  sharedTarget?: boolean;
  /** Names a new service cannot take. */
  takenNames?: readonly string[];
  /**
   * Variable name to the display name of the administrator's service that sets
   * it in the sandbox. A placeholder with one of these names gets a warning,
   * since the administrator's value wins. Leave it out on the dashboard.
   */
  configPlaceholders?: Readonly<Record<string, string>>;
  /**
   * The services the agent already has, to warn when the form covers a website
   * one of them already sends a key to. Leave it out on the dashboard.
   */
  agentServices?: readonly EgressCredentialService[];
  /** Whose store the service lives in, and the agent it is being added to, for the note under the title. */
  scope?: EgressScope;
  agentName?: string;
  /** For a `user` scope: the display names of the agents that share the store. */
  scopeAgents?: readonly string[];
  /** Fetch the server's presets and account logins when the form opens. */
  loadPresets: (signal: AbortSignal) => Promise<EgressPresetCatalog>;
  /**
   * Said before a save when saving has a wider effect, such as writing other
   * unsaved settings too; the user confirms once before the save goes out.
   */
  saveWarning?: string | null;
  /** Called when the form starts or stops differing from what it opened with. */
  onDirtyChange?: (dirty: boolean) => void;
  /**
   * Save the service; reject with an Error whose message is shown in the form.
   * Do not close the editor here: `onClose` does that once the key, when one
   * was typed, is saved too.
   */
  onSave: (name: string, service: AuthoredEgressService) => Promise<void>;
  /**
   * Save the API key typed in the form for the service of that name, called
   * after `onSave` succeeds and only when a key was typed. Without it the form
   * has no key field. When it rejects, the service stays saved and the form
   * says that only the key failed.
   */
  saveKey?: (key: string, name: string) => Promise<void>;
  /** Delete the service being edited; reject like `onSave`. Without it there is no delete button. */
  onDelete?: () => Promise<void>;
  /** Close the editor: on Cancel, and once the service, and its key when one was typed, are saved. */
  onClose: () => void;
}

/**
 * Form for one egress service: its preset or rules, and its placeholders.
 *
 * Used on the personal Connections page (the caller's own services) and on the
 * dashboard (services in config.yaml). It always hands back the authored form.
 * The basic fields come first; everything else sits under More options.
 */
export function EgressServiceEditor({
  service,
  name,
  initialRestrictOnly = false,
  context,
  sharedTarget = false,
  takenNames = [],
  configPlaceholders = {},
  agentServices = [],
  scope,
  agentName,
  scopeAgents = [],
  loadPresets,
  saveWarning = null,
  onDirtyChange,
  onSave,
  saveKey,
  onDelete,
  onClose,
}: EgressServiceEditorProps) {
  const id = useId();
  const editing = name !== undefined;
  // `null` while the presets load, then the server's list (empty when it could not be loaded).
  const [presets, setPresets] = useState<EgressPreset[] | null>(null);
  const [providers, setProviders] = useState<EgressOAuthProviderOption[]>([]);
  const [presetsFailed, setPresetsFailed] = useState(false);
  // What the server checks on save, sent with the presets; `null` until then, and when they could not be loaded.
  const [validation, setValidation] = useState<EgressValidation | null>(null);
  const options: EditorOptions = {
    context,
    sharedTarget,
    presets,
    validation,
  };
  // Checked against the names the editor opened with: a save that is still
  // running may already have written this name into the page's own list.
  const [openedWithNames] = useState(takenNames);
  const addingLimit = initialRestrictOnly && !editing;
  // A new service is named after its preset or host until the user types a
  // name; a new limit's name counts as typed.
  const [nameTyped, setNameTyped] = useState(addingLimit);
  const [form, setForm] = useState(() => {
    if (addingLimit)
      return {
        ...formFromService(limitedServiceName(openedWithNames), service),
        restrictOnly: true,
        rules: [],
      };
    const opened = formFromService(name ?? "", service);
    return editing
      ? opened
      : { ...opened, name: suggestedName(opened, openedWithNames) };
  });
  // A service that only limits repositories has no key, login, or website of its own.
  const limiting = form.restrictOnly;
  const [apiKey, setApiKey] = useState("");
  const [moreOpen, setMoreOpen] = useState(() => usesMoreOptions(form));
  const loadPresetsRef = useRef(loadPresets);
  const openedWith = useRef(form);
  // The repositories typed in the helper, which a save applies when the person
  // did not. A limit that is edited starts with the ones it is limited to, and
  // with the labels the helper wrote for them, which follow a changed list.
  // A limit the helper cannot write back exactly keeps its own rules, which
  // the rule fields edit instead.
  const [restored] = useState(() => restoredHelperInput(service));
  const helperLabels = useRef<HelperLabels>(restored.labels);
  const [helperInput, setHelperInput] = useState<HelperInput>(restored.input);
  const [helperErrors, setHelperErrors] = useState<string[]>([]);
  // The choices the form's rules were last written from, so a save writes
  // them anew only after they change and keeps rules edited by hand until then.
  const appliedHelper = useRef<string | null>(restored.applied);
  const showGithubHelper = limiting ? restored.exact : looksLikeGithub(form);
  const settings = { editing, takenNames: openedWithNames, service };

  // Writes the typed repositories into the form, or shows why they cannot be.
  const applyHelper = (): EgressServiceForm | null => {
    // A limit always covers uploads.github.com: it narrows only the hosts it
    // has rules for, so the administrator's uploads.github.com rule would
    // otherwise keep reaching every repository with the user's key or account.
    const result = githubRepositoryRules(helperInput.repositories, {
      ...helperInput,
      uploads: limiting || helperInput.uploads,
    });
    setHelperErrors(result.errors);
    if (result.errors.length > 0) return null;
    const { changes, written } = githubHelperChanges(
      form,
      result,
      helperLabels.current,
    );
    helperLabels.current = written;
    appliedHelper.current = helperSignature(helperInput);
    // The helper fixed the name along with the rules, so a later edit of the host does not rename it.
    if (form.name !== "") setNameTyped(true);
    // What was generated sits under More options, so show it.
    if (!limiting) setMoreOpen(true);
    setForm((previous) => ({ ...previous, ...changes }));
    return { ...form, ...changes };
  };

  // Typed repositories count even when "Use these repositories" was not
  // pressed, and a service that limits repositories needs some.
  const prepareForm = (): EgressServiceForm | null => {
    const typed = helperInput.repositories.trim() !== "";
    if (!showGithubHelper || (!typed && !limiting)) return form;
    if (typed && helperSignature(helperInput) === appliedHelper.current)
      return form;
    return applyHelper();
  };

  const formRef = useRef<HTMLFormElement>(null);
  const [attempted, setAttempted] = useState(false);
  const [focusRequest, setFocusRequest] = useState(0);
  const [serverError, setServerError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | "delete" | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingSave, setConfirmingSave] = useState(false);
  // The service as last written, so a retry after a failed key does not write
  // it again; once set, its name can no longer change.
  const [savedSignature, setSavedSignature] = useState<string | null>(null);
  // The form's problems, empty until the user has tried to save.
  const problems = attempted ? validateService(form, options, settings) : [];
  const dirty = useMemo(
    () =>
      JSON.stringify(form) !== JSON.stringify(openedWith.current) ||
      apiKey !== "" ||
      // Text in a helper that is not shown is not a change.
      (showGithubHelper &&
        helperSignature(helperInput) !== helperSignature(restored.input)),
    [form, apiKey, helperInput, restored, showGithubHelper],
  );
  const nameLocked = editing || savedSignature !== null;
  const disabled = busy !== null;

  useEffect(() => {
    const controller = new AbortController();
    loadPresetsRef
      .current(controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted) return;
        setPresets(loaded.presets);
        setProviders(loaded.oauth_providers);
        setValidation(loaded.validation);
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
  // After a failed save, the first invalid field in page order gets focus; a
  // problem that no field holds marks its message for focus instead.
  useEffect(() => {
    if (focusRequest === 0) return;
    formRef.current
      ?.querySelector<HTMLElement>(
        '[aria-invalid="true"], [data-problem-focus]',
      )
      ?.focus();
  }, [focusRequest]);

  const set = (changes: Partial<EgressServiceForm>) => {
    setConfirmingSave(false);
    setForm((previous) => {
      const next = { ...previous, ...changes };
      return nameLocked || nameTyped || "name" in changes
        ? next
        : { ...next, name: suggestedName(next, openedWithNames) };
    });
  };
  const setRule = (index: number, rule: RuleForm) => {
    const rules = [...form.rules];
    rules[index] = rule;
    set({ rules });
  };

  const preset = presetOf(presets, form.preset);
  // Shown beside the preset, since it is the preset that brings these variables.
  const presetWarnings = presetPlaceholderWarnings(
    form,
    preset,
    configPlaceholders,
  );
  const problemAt = (field: string) =>
    problems.find((problem) => problem.field === field)?.message;
  // The size of the whole service belongs to no field, so it is the one problem told in the alert.
  const generalErrors =
    problems.length > 0
      ? problems
          .filter((problem) => problem.field === "form")
          .map((problem) => problem.message)
      : serverError
        ? [serverError]
        : [];
  const title = limiting
    ? editing
      ? "Change limit"
      : "Limit to repositories"
    : editing
      ? `Edit ${name}`
      : "Add service";
  const note =
    context === "personal" && agentName !== undefined
      ? scopeNote(scope, agentName, scopeAgents)
      : null;
  // Deleting takes the service from everyone the store serves, so say who that is.
  const deleteNote =
    context === "personal" && scope !== undefined && agentName !== undefined
      ? (limiting ? removeLimitWarning : deleteServiceWarning)(
          scope,
          agentName,
          scopeAgents,
        )
      : null;
  // The services this form would send a key to the same website as, for this agent.
  const overlaps = limiting
    ? []
    : overlapWarnings(form, preset, agentServices, name);
  // Another service of the user's with rules on the hosts a limit, or the
  // repositories applied to this service, cover.
  const overlapHosts = !limiting
    ? [GITHUB_API_HOST, GITHUB_WEB_HOST]
    : showGithubHelper
      ? [GITHUB_API_HOST, GITHUB_UPLOADS_HOST, GITHUB_WEB_HOST]
      : (usesOwnRules(form) ? form.rules : (preset?.rules ?? [])).map(
          (rule) => rule.host,
        );
  const overlap = limitOverlap(
    overlapHosts,
    agentServices.filter((other) => other.name !== name),
  );
  const primaryRule = form.rules[0] ?? emptyRule();
  const ruleEditing = (index: number, rule: RuleForm): RuleEditing => ({
    index,
    rule,
    onChange: (next) => setRule(index, next),
    errorFor: (part: RuleFieldPart) => problemAt(ruleField(index, part)),
    disabled,
  });

  // Another preset has its own defaults, so what the loaded service emptied on
  // purpose no longer applies to it.
  const choosePreset = (value: string) => {
    // The rules the helper wrote are gone with the preset's own, so typed
    // repositories are to be applied again when the form is saved.
    appliedHelper.current = null;
    set({
      preset: value,
      replacePresetRules: false,
      rules: form.rules.length > 0 ? form.rules : [emptyRule()],
      explicitlyEmpty: NOT_EXPLICITLY_EMPTY,
    });
  };

  // A key never goes out for a service whose save failed. When only the key
  // fails, the service stays saved and a retry writes the key alone, unless the
  // service changed meanwhile.
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setAttempted(true);
    setServerError(null);
    const current = prepareForm();
    if (current === null) {
      setFocusRequest((count) => count + 1);
      return;
    }
    const found = validateService(current, options, settings);
    if (found.length > 0) {
      if (found.some(needsMoreOptions)) setMoreOpen(true);
      setFocusRequest((count) => count + 1);
      return;
    }
    if (saveWarning && !confirmingSave) {
      setConfirmingSave(true);
      return;
    }
    setConfirmingSave(false);
    setBusy("save");
    // A limit saved without a change goes back exactly as it was stored, so a
    // preset stays a preset and nothing the form cannot show is lost.
    const authored =
      limiting && service !== null && !dirty
        ? service
        : serializeService(current, options, service);
    const signature = JSON.stringify([current.name, authored]);
    try {
      // The service goes first, and the key only after it is written.
      if (signature !== savedSignature) {
        await onSave(current.name, authored);
        setSavedSignature(signature);
      }
    } catch (cause) {
      setServerError(
        errorText(cause, "Could not save the service. Try again."),
      );
      setBusy(null);
      return;
    }
    if (!limiting && saveKey && apiKey.trim() !== "") {
      try {
        await saveKey(apiKey, current.name);
      } catch (cause) {
        const reason = errorText(cause, "Could not save the key.");
        setServerError(
          `The service was saved, but its key was not: ${reason}${reason.endsWith(".") ? "" : "."} Paste the key again and save to retry.`,
        );
        setBusy(null);
        return;
      }
    }
    onClose();
  };

  const remove = async () => {
    setBusy("delete");
    setServerError(null);
    try {
      await onDelete?.();
    } catch (cause) {
      setServerError(
        errorText(cause, "Could not delete the service. Try again."),
      );
      setBusy(null);
      setConfirmingDelete(false);
    }
  };

  return (
    <form
      ref={formRef}
      aria-label={title}
      noValidate
      className="space-y-5 bg-muted/20 px-5 py-4"
      onSubmit={(event) => void submit(event)}
    >
      <h3 className="font-semibold">{title}</h3>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}

      {!limiting && (
        <>
          <Field label="Preset" id={`${id}-preset`} className="max-w-md">
            {(control) => (
              <select
                {...control}
                className={selectClass}
                value={form.preset}
                disabled={disabled || presets === null}
                onChange={(event) => choosePreset(event.target.value)}
              >
                <option value="">
                  {presets === null ? "Loading…" : "Custom"}
                </option>
                {(presets ?? []).map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.display_name}
                  </option>
                ))}
                {form.preset !== "" && !preset && (
                  <option value={form.preset}>{form.preset}</option>
                )}
              </select>
            )}
          </Field>
          {presetsFailed && (
            <p className="text-xs text-muted-foreground">
              The preset list could not be loaded. You can still use a custom
              service.
            </p>
          )}
          {preset && !form.replacePresetRules && (
            <p className="text-xs text-muted-foreground">
              The {preset.display_name} preset covers{" "}
              {preset.rules.map(ruleCoverage).join(", ")}.
            </p>
          )}
          {presetWarnings.map((warning) => (
            <p key={warning} className={warningClass}>
              {warning}
            </p>
          ))}
          {usesOwnRules(form) && (
            <div className="grid gap-3 sm:grid-cols-2">
              <RuleHostField {...ruleEditing(0, primaryRule)} />
              <RuleSendFields {...ruleEditing(0, primaryRule)} />
            </div>
          )}
          {overlaps.map((warning) => (
            <p key={warning} role="status" className={warningClass}>
              {warning}
            </p>
          ))}
        </>
      )}
      <Field
        label="Name"
        id={`${id}-name`}
        hint={
          editing
            ? "The name cannot be changed."
            : "Lowercase letters, digits, - and _. It cannot be changed later."
        }
        error={problemAt("name")}
        className="max-w-md"
      >
        {(control) => (
          <Input
            {...control}
            className={inputClass}
            value={form.name}
            disabled={nameLocked || disabled}
            onChange={(event) => {
              setNameTyped(event.target.value !== "");
              set({ name: event.target.value });
            }}
          />
        )}
      </Field>
      {saveKey && !limiting && (
        <Field
          label="API key"
          id={`${id}-key`}
          hint="Paste the key now or later"
          className="max-w-md"
        >
          {(control) => (
            <Input
              {...control}
              type="password"
              autoComplete="new-password"
              className={inputClass}
              value={apiKey}
              disabled={disabled}
              onChange={(event) => {
                setConfirmingSave(false);
                setApiKey(event.target.value);
              }}
            />
          )}
        </Field>
      )}
      {showGithubHelper && (
        <GithubHelper
          input={helperInput}
          onInput={(next) => {
            setConfirmingSave(false);
            setHelperErrors([]);
            setHelperInput(next);
          }}
          errors={helperErrors}
          onApply={limiting ? undefined : applyHelper}
          limit={limiting}
          overlap={overlap}
          disabled={disabled}
        />
      )}
      {limiting && !showGithubHelper && (
        <>
          {usesOwnRules(form) ? (
            <>
              <div className="max-w-md">
                <RuleHostField {...ruleEditing(0, primaryRule)} />
              </div>
              <RulesSection
                id={`${id}-limit-rules`}
                form={form}
                set={set}
                problemAt={problemAt}
                disabled={disabled}
                context={context}
                validation={validation}
                ruleEditing={ruleEditing}
                sendsKey={false}
              />
            </>
          ) : (
            // The form writes a limit's own rules, never a preset's, so these are only shown.
            <p className="text-xs text-muted-foreground">
              This limit uses the rules of the{" "}
              {preset?.display_name ?? form.preset} preset
              {preset
                ? `, which cover ${preset.rules.map(ruleCoverage).join(", ")}`
                : ""}
              . They cannot be changed here.
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Requests to these hosts whose path matches none of these rules are
            refused. {overlap ? `${overlap} ` : ""}Your key or connected account
            stays the same.
          </p>
        </>
      )}
      {limiting && scope !== undefined && context === "personal" && (
        <p className="text-xs text-muted-foreground">
          {administratorLimitNote(scope)}
        </p>
      )}

      {!limiting && (
        <section className="space-y-3">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            aria-expanded={moreOpen}
            aria-controls={moreOpen ? `${id}-more` : undefined}
            onClick={() => setMoreOpen((open) => !open)}
          >
            More options
          </Button>
          {moreOpen && (
            <MoreOptions
              id={`${id}-more`}
              form={form}
              set={set}
              problemAt={problemAt}
              disabled={disabled}
              context={context}
              scope={scope}
              sharedTarget={sharedTarget}
              validation={validation}
              preset={preset}
              providers={providers}
              onGithub={showGithubHelper}
              configPlaceholders={configPlaceholders}
              ruleEditing={ruleEditing}
              newService={service === null}
            />
          )}
        </section>
      )}

      {generalErrors.length > 0 && (
        <Alert variant="destructive" className="p-2">
          <AlertDescription>
            {generalErrors.length === 1 ? (
              generalErrors[0]
            ) : (
              <ul className="list-disc pl-4">
                {generalErrors.map((error, index) => (
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
        {/* The presets bring the validation a save is checked against, so saving waits until they have loaded or failed. */}
        <Button type="submit" size="sm" disabled={disabled || presets === null}>
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
          ) : limiting ? (
            "Save limit"
          ) : (
            "Save service"
          )}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={disabled}
          onClick={confirmingSave ? () => setConfirmingSave(false) : onClose}
        >
          {confirmingSave ? "Go back" : "Cancel"}
        </Button>
        {editing && onDelete && !confirmingDelete && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={disabled}
            onClick={() => setConfirmingDelete(true)}
          >
            {limiting ? "Remove limit" : "Delete service"}
          </Button>
        )}
        {editing && onDelete && confirmingDelete && (
          <span className="ml-auto flex flex-wrap items-center gap-2 text-xs">
            {limiting ? "Remove limit?" : `Delete ${name}?`}
            {deleteNote ? ` ${deleteNote}` : ""}
            {saveWarning ? ` ${saveWarning}` : ""}
            <Button
              type="button"
              size="sm"
              variant="destructive"
              disabled={disabled}
              onClick={() => void remove()}
            >
              {busy === "delete"
                ? limiting
                  ? "Removing…"
                  : "Deleting…"
                : limiting
                  ? "Confirm remove"
                  : "Confirm delete"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={disabled}
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
