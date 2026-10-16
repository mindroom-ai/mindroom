import type { EgressAuth, EgressRule } from "./types";

/**
 * The rules the repository helper writes: which GitHub hosts and paths a
 * service may reach for the `owner/repo` lines it is given, and the labels a
 * service limited to them gets. `githubLimits.ts` reads such rules back.
 */

export const GITHUB_API_HOST = "api.github.com";
export const GITHUB_WEB_HOST = "github.com";
export const GITHUB_UPLOADS_HOST = "uploads.github.com";
export const GRAPHQL_PATH = "/graphql";
const GITHUB_REPOSITORY_LINE =
  /^([A-Za-z0-9][A-Za-z0-9-]*)\/(\*|[A-Za-z0-9._-]+)$/;
const GITHUB_MAX_OWNER_LENGTH = 39;
const GITHUB_MAX_REPOSITORY_LENGTH = 100;

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
): { rules: EgressRule[]; errors: string[]; repositories: string[] } {
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
    if (
      match[1].length > GITHUB_MAX_OWNER_LENGTH ||
      repo.length > GITHUB_MAX_REPOSITORY_LENGTH
    ) {
      errors.push(
        `${text} is too long: GitHub owners have at most ${GITHUB_MAX_OWNER_LENGTH} characters and repositories ${GITHUB_MAX_REPOSITORY_LENGTH}`,
      );
      continue;
    }
    targets.set(`${match[1]}/${repo}`, { owner: match[1], repo });
  }
  if (targets.size === 0 && errors.length === 0)
    errors.push("Enter at least one repository, such as owner/repo");
  if (errors.length > 0) return { rules: [], errors, repositories: [] };
  const rules: EgressRule[] = [];
  for (const { owner, repo } of targets.values()) {
    // An owner's prefix ends in a slash, which matches every path below it.
    const target = repo === "*" ? `${owner}/` : `${owner}/${repo}`;
    rules.push(
      { host: GITHUB_API_HOST, path_prefix: `/repos/${target}`, auth: bearer },
      { host: GITHUB_WEB_HOST, path_prefix: `/${target}`, auth: git },
    );
    if (repo !== "*")
      rules.push({
        host: GITHUB_WEB_HOST,
        path_prefix: `/${target}.git`,
        auth: git,
      });
    if (options.uploads)
      rules.push({
        host: GITHUB_UPLOADS_HOST,
        path_prefix: `/repos/${target}`,
        auth: bearer,
      });
  }
  if (options.graphql)
    rules.push({
      host: GITHUB_API_HOST,
      path_prefix: GRAPHQL_PATH,
      auth: bearer,
    });
  return { rules, errors, repositories: [...targets.keys()] };
}

/** The display name and description for a service limited to these repositories. */
export function githubLimitLabels(repositories: readonly string[]): {
  displayName: string;
  description: string;
} {
  const repos = repositories.join(", ");
  return {
    displayName: `GitHub (${repos})`,
    description: `Limited to ${repos}`,
  };
}
