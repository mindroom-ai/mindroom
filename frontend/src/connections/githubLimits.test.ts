import { describe, expect, it } from "vitest";
import { type EgressServiceForm, formFromService } from "./egressServiceForm";
import { validRule } from "@/test/egressServiceTestKit";
import {
  coversGithub,
  limitedRepositories,
  looksLikeGithub,
  parseGithubLimit,
} from "./githubLimits";
import { githubRepositoryRules } from "./githubRepositoryRules";
import type { EgressRuleSummary } from "./types";

const rule = (host: string, path_prefix = "/"): EgressRuleSummary => ({
  host,
  port: null,
  path_prefix,
});
const summarized = (input: string, uploads = false, graphql = false) =>
  githubRepositoryRules(input, { uploads, graphql }).rules.map((item) =>
    rule(item.host, item.path_prefix),
  );

describe("GitHub coverage", () => {
  it("sees the API, web, and upload hosts, in any case", () => {
    expect(coversGithub([rule("api.github.com")])).toBe(true);
    expect(coversGithub([rule("GitHub.com")])).toBe(true);
    expect(coversGithub([rule("uploads.github.com")])).toBe(true);
    expect(
      coversGithub([rule("httpbin.org"), rule("github.example.com")]),
    ).toBe(false);
    expect(coversGithub([])).toBe(false);
  });
});

describe("repositories a service is limited to", () => {
  it("reads back what the editor's helper writes", () => {
    expect(limitedRepositories(summarized("basnijholt/agent-cli"))).toEqual([
      "basnijholt/agent-cli",
    ]);
    expect(limitedRepositories(summarized("a/b\nc/d", true))).toEqual([
      "a/b",
      "c/d",
    ]);
    expect(limitedRepositories(summarized("octo/*\na/b", true))).toEqual([
      "octo/*",
      "a/b",
    ]);
  });

  it("does not call a service limited while it reaches more", () => {
    expect(
      limitedRepositories([rule("api.github.com"), rule("github.com")]),
    ).toEqual([]);
    expect(limitedRepositories(summarized("a/b", false, true))).toEqual([]);
    expect(
      limitedRepositories([...summarized("a/b"), rule("api.github.com", "/")]),
    ).toEqual([]);
  });

  it("ignores rules on other hosts and reads none from a service without GitHub rules", () => {
    expect(
      limitedRepositories([...summarized("a/b"), rule("httpbin.org")]),
    ).toEqual(["a/b"]);
    expect(limitedRepositories([rule("httpbin.org", "/repos/a/b")])).toEqual(
      [],
    );
  });

  it("takes a path of the wrong shape for the host as no repository", () => {
    expect(limitedRepositories([rule("api.github.com", "/a/b")])).toEqual([]);
    expect(limitedRepositories([rule("github.com", "/repos/a/b")])).toEqual([]);
    expect(limitedRepositories([rule("github.com", "/a/b/")])).toEqual([]);
  });
});

describe("the choices behind a limit's rules", () => {
  it("reads back repositories, owners, and the options the helper wrote", () => {
    expect(parseGithubLimit(summarized("a/b\nc/d", true, true))).toEqual({
      repositories: ["a/b", "c/d"],
      graphql: true,
      exact: true,
    });
    expect(parseGithubLimit(summarized("octo/*\na/b"))).toEqual({
      repositories: ["octo/*", "a/b"],
      graphql: false,
      exact: true,
    });
  });

  it("reads past GraphQL, which makes limitedRepositories give up", () => {
    const rules = summarized("a/b", false, true);
    expect(limitedRepositories(rules)).toEqual([]);
    expect(parseGithubLimit(rules).repositories).toEqual(["a/b"]);
  });

  it("is not exact when a rule is not one the helper writes, and still names the repositories it can", () => {
    const odd = parseGithubLimit([
      ...summarized("a/b"),
      rule("api.github.com", "/user"),
      rule("httpbin.org", "/"),
    ]);
    expect(odd.repositories).toEqual(["a/b"]);
    expect(odd.exact).toBe(false);
    // A rule missing from what the helper would write is not exact either.
    expect(parseGithubLimit(summarized("a/b").slice(0, 2)).exact).toBe(false);
    // Nor is a rule with a port, which the helper never writes.
    expect(
      parseGithubLimit(
        summarized("a/b").map((item, index) =>
          index === 0 ? { ...item, port: 8443 } : item,
        ),
      ).exact,
    ).toBe(false);
  });

  it("has nothing to read in rules that name no repository", () => {
    expect(parseGithubLimit([rule("api.github.com", "/")])).toEqual({
      repositories: [],
      graphql: false,
      exact: false,
    });
  });
});

describe("offering the GitHub helper", () => {
  const withHost = (
    host: string,
    changes: Partial<EgressServiceForm> = {},
  ) => ({
    ...formFromService("svc", null),
    rules: [{ ...validRule(), host }],
    ...changes,
  });

  it("is for the GitHub preset and for rules on a GitHub host", () => {
    expect(looksLikeGithub(formFromService("svc", { preset: "github" }))).toBe(
      true,
    );
    for (const host of ["github.com", "api.github.com", "uploads.github.com"])
      expect(looksLikeGithub(withHost(host))).toBe(true);
    expect(looksLikeGithub(withHost(" API.GitHub.com "))).toBe(true);
  });

  it("is hidden for other hosts and for an empty host", () => {
    expect(looksLikeGithub(withHost("httpbin.org"))).toBe(false);
    expect(looksLikeGithub(withHost(""))).toBe(false);
    expect(looksLikeGithub(withHost("gist.github.com"))).toBe(false);
  });

  it("looks at the rules only while the service uses its own", () => {
    const kept = withHost("github.com", { preset: "openai" });
    expect(looksLikeGithub(kept)).toBe(false);
    expect(looksLikeGithub({ ...kept, replacePresetRules: true })).toBe(true);
    expect(
      looksLikeGithub({ ...withHost("httpbin.org"), preset: "github" }),
    ).toBe(true);
  });
});
