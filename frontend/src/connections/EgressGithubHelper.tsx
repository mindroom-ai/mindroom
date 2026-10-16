import { useId } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  BROKERED_PLACEHOLDER,
  type EgressServiceForm,
  type PlaceholderForm,
  ruleFormFrom,
} from "./egressServiceForm";
import { githubLimitLabels } from "./githubRepositoryRules";
import { parseGithubLimit } from "./githubLimits";
import type { AuthoredEgressService, EgressRule } from "./types";

const GITHUB_PLACEHOLDER_NAMES = ["GH_TOKEN", "GITHUB_TOKEN"];
const brokered = (name: string): PlaceholderForm => ({
  name,
  value: BROKERED_PLACEHOLDER,
});

/** What the person chose in the helper. */
export interface HelperInput {
  /** One `owner/repo` or `owner/*` per line. */
  repositories: string;
  uploads: boolean;
  graphql: boolean;
}

export const EMPTY_HELPER_INPUT: HelperInput = {
  repositories: "",
  uploads: false,
  graphql: false,
};

/**
 * The choices behind a limit service's rules, for opening it again, whether the
 * helper can write exactly those rules back, the labels it wrote, which the
 * next apply may rewrite, and the signature of the choices the rules count as
 * written from (`applied`), so a save writes them anew only when it is null or
 * the choices change. When the helper cannot write the rules back, changing
 * the repositories replaces rules the list does not show. Labels the person
 * wrote themselves are not the helper's, so they stay.
 */
export function restoredHelperInput(service: AuthoredEgressService | null): {
  input: HelperInput;
  exact: boolean;
  labels: HelperLabels;
  applied: string | null;
} {
  if (service?.restrict_only !== true)
    return {
      input: EMPTY_HELPER_INPUT,
      exact: true,
      labels: { displayName: "", description: "" },
      applied: null,
    };
  const limit = parseGithubLimit(service.rules ?? []);
  const written = githubLimitLabels(limit.repositories);
  const input = {
    repositories: limit.repositories.join("\n"),
    uploads: true,
    graphql: limit.graphql,
  };
  return {
    input,
    exact: limit.exact,
    applied: input.repositories === "" ? null : helperSignature(input),
    labels: {
      displayName:
        service.display_name === written.displayName ? written.displayName : "",
      description:
        service.description === written.description ? written.description : "",
    },
  };
}

/** Tells two choices apart, so the editor applies typed repositories once, and again only after they change. */
export function helperSignature(input: HelperInput): string {
  return JSON.stringify([
    input.repositories.trim(),
    input.uploads,
    input.graphql,
  ]);
}

/** The labels the helper last wrote, which it may rewrite on the next apply. */
export type HelperLabels = ReturnType<typeof githubLimitLabels>;

/**
 * What applying the helper changes in the form, and the labels it wrote.
 *
 * It replaces the rules and turns on the restriction. A custom service also
 * gets the placeholders the GitHub preset would set, unless it already sets
 * some. Labels the user wrote stay; the ones the last apply wrote follow the
 * new repositories. The name is kept as it is, so the new first rule's host
 * does not rename the service.
 */
export function githubHelperChanges(
  form: EgressServiceForm,
  result: { rules: EgressRule[]; repositories: string[] },
  written: HelperLabels,
): { changes: Partial<EgressServiceForm>; written: HelperLabels } {
  const labels = githubLimitLabels(result.repositories);
  const fillDisplayName =
    form.displayName.trim() === "" || form.displayName === written.displayName;
  const fillDescription =
    form.description.trim() === "" || form.description === written.description;
  // A service that only limits paths has no key to tell the sandbox about.
  const addPlaceholders =
    form.preset === "" && form.placeholders.length === 0 && !form.restrictOnly;
  return {
    changes: {
      name: form.name,
      rules: result.rules.map(ruleFormFrom),
      replacePresetRules: form.preset !== "",
      restrictToRules: true,
      ...(fillDisplayName ? { displayName: labels.displayName } : {}),
      ...(fillDescription ? { description: labels.description } : {}),
      ...(addPlaceholders
        ? { placeholders: GITHUB_PLACEHOLDER_NAMES.map(brokered) }
        : {}),
    },
    written: {
      displayName: fillDisplayName ? labels.displayName : written.displayName,
      description: fillDescription ? labels.description : written.description,
    },
  };
}

export function GithubHelper({
  input,
  onInput,
  errors,
  onApply,
  limit = false,
  overlap = null,
  disabled = false,
}: {
  input: HelperInput;
  onInput: (input: HelperInput) => void;
  /** What is wrong with the repositories, shown under them. */
  errors: readonly string[];
  /** Apply the repositories now; without it they are applied when the service is saved. */
  onApply?: () => void;
  /** The service only limits paths, so the text says what the limit does and not what is replaced. */
  limit?: boolean;
  /** The note `limitOverlap` gives on the user's other services with rules on these hosts, which the text adds. */
  overlap?: string | null;
  /** While the service saves or deletes, so an edit made then is not lost when the editor closes. */
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <section
      aria-labelledby={`${id}-heading`}
      className="space-y-3 rounded-md border border-border/60 bg-muted/20 p-3"
    >
      <div className="space-y-1">
        <h4 id={`${id}-heading`} className="text-sm font-medium">
          {/* The form's own title says what the limit is. */}
          {limit ? "Repositories" : "Limit to repositories"}
        </h4>
        <p className="text-xs text-muted-foreground">
          One repository per line, as owner/repo, or owner/* for everything an
          owner has.{" "}
          {limit
            ? `Requests to api.github.com, uploads.github.com, and github.com (API, release assets, and git over HTTPS) for any other repository are refused, and so are other API paths, such as /user or search. ${overlap ? `${overlap} ` : ""}Your key or connected account stays the same.`
            : `Applying replaces the rules above with rules for just those repositories (API and git over HTTPS) and refuses every other path on api.github.com and github.com. ${overlap ?? "Other API paths, such as /user or search, stop working."}`}{" "}
          Matching is case sensitive, so write names as they appear in GitHub
          URLs.
        </p>
      </div>
      <Textarea
        aria-label="GitHub repositories"
        aria-invalid={errors.length > 0 ? true : undefined}
        placeholder={"basnijholt/agent-cli\nmindroom-ai/*"}
        rows={3}
        className="font-mono text-xs aria-invalid:border-destructive"
        disabled={disabled}
        value={input.repositories}
        onChange={(event) =>
          onInput({ ...input, repositories: event.target.value })
        }
      />
      {/* A limit always covers uploads.github.com, or that host would stay open. */}
      {!limit && (
        <label className="flex items-start gap-2 text-xs">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4"
            checked={input.uploads}
            disabled={disabled}
            onChange={(event) =>
              onInput({ ...input, uploads: event.target.checked })
            }
          />
          <span>
            Also allow uploads.github.com for these repositories (release
            assets)
          </span>
        </label>
      )}
      <label className="flex items-start gap-2 text-xs">
        <input
          type="checkbox"
          className="mt-0.5 h-4 w-4"
          checked={input.graphql}
          disabled={disabled}
          onChange={(event) =>
            onInput({ ...input, graphql: event.target.checked })
          }
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
      {onApply && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={onApply}
        >
          Use these repositories
        </Button>
      )}
    </section>
  );
}
