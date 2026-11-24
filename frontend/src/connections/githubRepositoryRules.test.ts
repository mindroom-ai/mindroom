import { describe, expect, it } from "vitest";
import {
  githubLimitLabels,
  githubRepositoryRules,
} from "./githubRepositoryRules";

describe("GitHub repository rules", () => {
  const none = { uploads: false, graphql: false };

  it("generates the API and git rules for one repository", () => {
    expect(githubRepositoryRules("basnijholt/agent-cli", none)).toEqual({
      errors: [],
      repositories: ["basnijholt/agent-cli"],
      rules: [
        {
          host: "api.github.com",
          path_prefix: "/repos/basnijholt/agent-cli",
          auth: { type: "bearer" },
        },
        {
          host: "github.com",
          path_prefix: "/basnijholt/agent-cli",
          auth: { type: "basic", username: "x-access-token" },
        },
        {
          host: "github.com",
          path_prefix: "/basnijholt/agent-cli.git",
          auth: { type: "basic", username: "x-access-token" },
        },
      ],
    });
  });

  it("makes an owner-wide rule with a trailing slash for owner/*", () => {
    expect(githubRepositoryRules("mindroom-ai/*", none)).toEqual({
      errors: [],
      repositories: ["mindroom-ai/*"],
      rules: [
        {
          host: "api.github.com",
          path_prefix: "/repos/mindroom-ai/",
          auth: { type: "bearer" },
        },
        {
          host: "github.com",
          path_prefix: "/mindroom-ai/",
          auth: { type: "basic", username: "x-access-token" },
        },
      ],
    });
  });

  it("adds uploads.github.com for the same repositories only when ticked", () => {
    const hosts = (uploads: boolean, input: string) =>
      githubRepositoryRules(input, { uploads, graphql: false }).rules.filter(
        (rule) => rule.host === "uploads.github.com",
      );
    expect(hosts(false, "o/r")).toEqual([]);
    expect(hosts(true, "o/r")).toEqual([
      {
        host: "uploads.github.com",
        path_prefix: "/repos/o/r",
        auth: { type: "bearer" },
      },
    ]);
    expect(hosts(true, "o/*")).toEqual([
      {
        host: "uploads.github.com",
        path_prefix: "/repos/o/",
        auth: { type: "bearer" },
      },
    ]);
  });

  it("writes each repository's uploads.github.com rule after its others, and GraphQL last", () => {
    const written = (graphql: boolean) =>
      githubRepositoryRules("a/b\nc/*", { uploads: true, graphql }).rules.map(
        (rule) => `${rule.host}${rule.path_prefix}`,
      );
    const repositoryRules = [
      "api.github.com/repos/a/b",
      "github.com/a/b",
      "github.com/a/b.git",
      "uploads.github.com/repos/a/b",
      "api.github.com/repos/c/",
      "github.com/c/",
      "uploads.github.com/repos/c/",
    ];
    expect(written(false)).toEqual(repositoryRules);
    expect(written(true)).toEqual([
      ...repositoryRules,
      "api.github.com/graphql",
    ]);
  });

  it("adds /graphql once, only when ticked", () => {
    const graphql = (tick: boolean) =>
      githubRepositoryRules("a/b\nc/*", {
        uploads: false,
        graphql: tick,
      }).rules.filter((rule) => rule.path_prefix === "/graphql");
    expect(graphql(false)).toEqual([]);
    expect(graphql(true)).toEqual([
      {
        host: "api.github.com",
        path_prefix: "/graphql",
        auth: { type: "bearer" },
      },
    ]);
  });

  it("ignores blank lines and repeats, trims, and drops a .git suffix", () => {
    const { rules, errors, repositories } = githubRepositoryRules(
      "\n  o/r  \r\no/r\no/r.git\n\n",
      none,
    );
    expect(errors).toEqual([]);
    expect(rules).toHaveLength(3);
    expect(rules[0].path_prefix).toBe("/repos/o/r");
    expect(repositories).toEqual(["o/r"]);
    expect(
      githubRepositoryRules("o/a\no/b.git\no/a\np/*", none).repositories,
    ).toEqual(["o/a", "o/b", "p/*"]);
  });

  it("names each line that is not owner/repo or owner/*", () => {
    expect(
      githubRepositoryRules(
        "https://github.com/o/r\nowner\no/r/extra\no/..\n",
        none,
      ),
    ).toEqual({
      rules: [],
      repositories: [],
      errors: [
        "https://github.com/o/r is not owner/repo or owner/*",
        "owner is not owner/repo or owner/*",
        "o/r/extra is not owner/repo or owner/*",
        "o/.. is not owner/repo or owner/*",
      ],
    });
    expect(githubRepositoryRules("  \n", none).errors).toEqual([
      "Enter at least one repository, such as owner/repo",
    ]);
  });
});

describe("labels from the repositories a service is limited to", () => {
  it("names them in the display name and the description", () => {
    expect(githubLimitLabels(["o/a"])).toEqual({
      displayName: "GitHub (o/a)",
      description: "Limited to o/a",
    });
    expect(githubLimitLabels(["o/a", "o/b", "p/*"])).toEqual({
      displayName: "GitHub (o/a, o/b, p/*)",
      description: "Limited to o/a, o/b, p/*",
    });
  });
});
