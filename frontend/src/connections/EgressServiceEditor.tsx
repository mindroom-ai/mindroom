import { type FormEvent, type ReactNode, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type {
  AuthoredEgressService,
  EgressAuth,
  EgressAuthType,
  EgressCredentialService,
  EgressRule,
} from "./types";

/** Where the editor runs: the personal page, or the dashboard writing config.yaml. */
export type EditorContext = "personal" | "operator";

interface EditorOptions {
  context: EditorContext;
  /** Shared and unscoped agents cannot use personal accounts, so OAuth is off. */
  sharedTarget: boolean;
}

interface EgressPresetOption {
  id: string;
  label: string;
  /** Hosts, with a path where the preset covers only part of one. */
  covers: string[];
  /** The MindRoom OAuth provider the preset signs in with, if any. */
  oauthProvider?: string;
}

// Mirrors src/mindroom/egress_broker/presets.py; the rules themselves stay on the server.
export const EGRESS_PRESETS: readonly EgressPresetOption[] = [
  {
    id: "github",
    label: "GitHub",
    covers: ["api.github.com", "uploads.github.com", "github.com"],
    oauthProvider: "github",
  },
  {
    id: "google_drive",
    label: "Google Drive",
    covers: ["www.googleapis.com/drive/", "www.googleapis.com/upload/drive/"],
    oauthProvider: "google_drive",
  },
  {
    id: "google_gmail",
    label: "Gmail",
    covers: ["gmail.googleapis.com", "www.googleapis.com/gmail/"],
    oauthProvider: "google_gmail",
  },
  {
    id: "google_calendar",
    label: "Google Calendar",
    covers: ["www.googleapis.com/calendar/"],
    oauthProvider: "google_calendar",
  },
  {
    id: "google_sheets",
    label: "Google Sheets",
    covers: ["sheets.googleapis.com"],
    oauthProvider: "google_sheets",
  },
  {
    id: "google_docs",
    label: "Google Docs",
    covers: ["docs.googleapis.com"],
    oauthProvider: "google_docs",
  },
  {
    id: "google_tasks",
    label: "Google Tasks",
    covers: ["tasks.googleapis.com", "www.googleapis.com/tasks/"],
    oauthProvider: "google_tasks",
  },
  {
    id: "atlassian",
    label: "Atlassian",
    covers: ["api.atlassian.com"],
    oauthProvider: "atlassian",
  },
  { id: "openai", label: "OpenAI", covers: ["api.openai.com"] },
  { id: "anthropic", label: "Anthropic", covers: ["api.anthropic.com"] },
];

const AUTH_TYPES: { value: EgressAuthType; label: string }[] = [
  { value: "bearer", label: "Bearer token" },
  { value: "basic", label: "Basic (username and key)" },
  { value: "header", label: "Header" },
  { value: "query", label: "Query parameter" },
];

const DEFAULT_TEMPLATE = "{secret}";
const BROKERED_PLACEHOLDER = "mindroom-brokered";
const MAX_RULES = 50;
const MAX_SERVICE_BYTES = 16 * 1024;
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const HEADER_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
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

interface RuleForm {
  host: string;
  port: string;
  pathPrefix: string;
  authType: EgressAuthType;
  username: string;
  headerOrParam: string;
  template: string;
}

interface PlaceholderForm {
  name: string;
  value: string;
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
}

function emptyRule(): RuleForm {
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

function ruleFormFrom(rule: EgressRule): RuleForm {
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
  return {
    name,
    preset,
    displayName: service?.display_name ?? "",
    description: service?.description ?? "",
    replacePresetRules,
    rules: preset === "" && rules.length === 0 ? [emptyRule()] : rules,
    placeholders: Object.entries(service?.placeholder_env ?? {}).map(
      ([placeholderName, value]) => ({ name: placeholderName, value }),
    ),
    restrictToRules: service?.restrict_to_rules ?? false,
    oauthProvider: service?.oauth_provider ?? "",
  };
}

function presetOf(id: string): EgressPresetOption | undefined {
  return EGRESS_PRESETS.find((preset) => preset.id === id);
}

function usesOwnRules(form: EgressServiceForm): boolean {
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
 * `oauth_on_shared_workers`, and an OAuth provider override on a preset service.
 */
export function serializeService(
  form: EgressServiceForm,
  options: EditorOptions,
  initial: AuthoredEgressService | null = null,
): AuthoredEgressService {
  const authored: AuthoredEgressService = {};
  if (form.preset) authored.preset = form.preset;
  if (form.displayName.trim()) authored.display_name = form.displayName.trim();
  if (form.description.trim()) authored.description = form.description.trim();
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
  if (form.preset === "") {
    if (!options.sharedTarget && form.oauthProvider.trim())
      authored.oauth_provider = form.oauthProvider.trim();
  } else if (options.sharedTarget && presetOf(form.preset)?.oauthProvider) {
    // A shared agent cannot use personal accounts, so the preset's login is dropped.
    authored.oauth_provider = null;
  } else if (initial?.oauth_provider !== undefined) {
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
    if (name === "" && placeholder.value === "") continue;
    if (!PLACEHOLDER_NAME_PATTERN.test(name)) {
      errors.push(
        `Placeholder name ${name || "(empty)"} must match ^[A-Z_][A-Z0-9_]*$`,
      );
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

/**
 * Name the administrator's service that most likely covers every GitHub path.
 *
 * The listing does not carry rules, so this recognizes the usual setup: a
 * config service called `github` or one that signs in with the GitHub
 * provider. Limiting repositories cannot narrow such a service.
 */
export function findBroaderGithubService(
  services: readonly EgressCredentialService[],
  editing?: string,
): string | null {
  const found = services.find(
    (service) =>
      service.source === "config" &&
      service.name !== editing &&
      (service.name === "github" || service.oauth?.provider === "github"),
  );
  return found ? found.display_name : null;
}

function looksLikeGithub(form: EgressServiceForm): boolean {
  if (form.preset === "github") return true;
  if (!usesOwnRules(form)) return false;
  if (form.preset === "")
    return (
      form.rules.every((rule) => rule.host.trim() === "") ||
      form.rules.some((rule) => GITHUB_HOSTS.includes(rule.host.trim()))
    );
  return form.rules.some((rule) => GITHUB_HOSTS.includes(rule.host.trim()));
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

function GithubHelper({
  broaderService,
  onApply,
}: {
  broaderService: string | null;
  onApply: (rules: EgressRule[]) => void;
}) {
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
      {broaderService && (
        // A standing notice, not something to announce on every render.
        <Alert role="note" className="p-2">
          <AlertDescription>
            {broaderService} is configured by your administrator and already
            covers every GitHub path, so this limit cannot narrow it. Requests
            that match its rules are still sent with its credentials.
          </AlertDescription>
        </Alert>
      )}
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
  /** The administrator's service that already covers every GitHub path, if there is one. */
  broaderGithubService?: string | null;
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
  broaderGithubService = null,
  onSave,
  onDelete,
  onCancel,
}: EgressServiceEditorProps) {
  const id = useId();
  const editing = name !== undefined;
  const options: EditorOptions = { context, sharedTarget };
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

  const set = (changes: Partial<EgressServiceForm>) =>
    setForm((previous) => ({ ...previous, ...changes }));
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

  const preset = presetOf(form.preset);
  const settings = { editing, takenNames: openedWithNames };
  const validation = attempted ? validateService(form, options, settings) : [];
  const shownErrors =
    validation.length > 0 ? validation : serverError ? [serverError] : [];
  const title = editing ? `Edit ${name}` : "Add service";
  const isPersonal = context === "personal";
  const showGithubHelper = looksLikeGithub(form);

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
            disabled={busy !== null}
            onChange={(event) => choosePreset(event.target.value)}
          >
            <option value="">Custom</option>
            {EGRESS_PRESETS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
            {form.preset !== "" && !preset && (
              <option value={form.preset}>{form.preset}</option>
            )}
          </select>
        </Field>
        <Field
          label="Display name"
          htmlFor={`${id}-display`}
          hint={preset ? `Leave empty to use ${preset.label}.` : undefined}
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
            The {preset.label} preset covers {preset.covers.join(", ")}.
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
            broaderService={broaderGithubService}
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
      </div>

      {preset?.oauthProvider ? (
        <p className="text-xs text-muted-foreground">
          {sharedTarget
            ? "Accounts cannot be connected on a shared agent, so this service uses an API key."
            : `Users can connect an account (${preset.oauthProvider}) instead of an API key.`}
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
                  ? ` Leave the list empty to use the placeholders of the ${preset.label} preset.`
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

      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" disabled={busy !== null}>
          {busy === "save" ? (
            <>
              <Loader2
                className="mr-2 h-3.5 w-3.5 animate-spin"
                aria-hidden="true"
              />
              Saving…
            </>
          ) : (
            "Save service"
          )}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy !== null}
          onClick={onCancel}
        >
          Cancel
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
            Delete {name}?
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
