import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { Knowledge } from "./Knowledge";
import { API_ENDPOINTS } from "@/lib/api";
import { useConfigStore } from "@/store/configStore";
import type { SaveConfigResult } from "@/store/configStore";
import type { Config, KnowledgeBaseConfig } from "@/types/config";

vi.mock("@/store/configStore", () => ({
  useConfigStore: vi.fn(),
}));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockUpdateKnowledgeBase = vi.fn();
const mockDeleteKnowledgeBase = vi.fn();
const mockSaveConfig = vi
  .fn<() => Promise<SaveConfigResult>>()
  .mockResolvedValue({ status: "saved" });

type KnowledgeApiPayloads = {
  status: {
    base_id: string;
    folder_path: string;
    watch: boolean;
    file_count: number;
    indexed_count: number;
    refreshing?: boolean;
    refresh_state?: "none" | "stale" | "refreshing" | "refresh_failed";
    refresh_job?: "idle" | "pending" | "running" | "failed";
    last_error?: string | null;
    file_listing_degraded?: boolean;
    file_listing_error?: string | null;
    git?: {
      repo_url: string;
      branch: string;
      lfs: boolean;
      syncing: boolean;
      repo_present: boolean;
      initial_sync_complete: boolean;
      last_successful_sync_at: string | null;
      last_successful_commit: string | null;
      last_error: string | null;
    };
  };
  files: {
    base_id: string;
    files: Array<{
      name: string;
      path: string;
      size: number;
      modified: string;
      type: string;
    }>;
    total_size: number;
    file_count: number;
    file_listing_degraded?: boolean;
    file_listing_error?: string | null;
  };
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferredJsonResponse(payload: unknown): {
  promise: Promise<Response>;
  resolve: () => void;
} {
  let resolve!: () => void;
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = () => resolvePromise(jsonResponse(payload));
  });
  return { promise, resolve };
}

function setKnowledgeApiMock(
  payloadByBase: Record<string, KnowledgeApiPayloads>,
  options: {
    reindexResponses?: Record<string, Response>;
  } = {},
) {
  const fetchMock = vi.mocked(global.fetch);
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);

    const statusMatch = url.match(/\/api\/knowledge\/bases\/([^/]+)\/status$/);
    if (statusMatch) {
      const baseId = decodeURIComponent(statusMatch[1] ?? "");
      const payload = payloadByBase[baseId]?.status;
      return Promise.resolve(
        payload
          ? jsonResponse(payload)
          : jsonResponse({ detail: "Not found" }, 404),
      );
    }

    const filesMatch = url.match(/\/api\/knowledge\/bases\/([^/]+)\/files$/);
    if (filesMatch) {
      const baseId = decodeURIComponent(filesMatch[1] ?? "");
      const payload = payloadByBase[baseId]?.files;
      return Promise.resolve(
        payload
          ? jsonResponse(payload)
          : jsonResponse({ detail: "Not found" }, 404),
      );
    }

    const reindexMatch = url.match(
      /\/api\/knowledge\/bases\/([^/]+)\/reindex$/,
    );
    if (reindexMatch) {
      const baseId = decodeURIComponent(reindexMatch[1] ?? "");
      return Promise.resolve(
        options.reindexResponses?.[baseId] ??
          jsonResponse({ success: true, indexed_count: 1 }),
      );
    }

    return Promise.resolve(
      jsonResponse({ detail: `Unhandled URL: ${url}` }, 404),
    );
  });
}

function mockStore(
  knowledgeBases: Record<string, KnowledgeBaseConfig>,
  options: { isDirty?: boolean } = {},
) {
  const storeMock = useConfigStore as unknown as Mock;
  storeMock.mockReturnValue({
    config: {
      knowledge_bases: knowledgeBases,
    } as unknown as Config,
    updateKnowledgeBase: mockUpdateKnowledgeBase,
    deleteKnowledgeBase: mockDeleteKnowledgeBase,
    saveConfig: mockSaveConfig,
    isDirty: options.isDirty ?? false,
  });
}

describe("Knowledge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not auto-select the first base when multiple bases are configured", async () => {
    mockStore({
      alpha: { path: "./knowledge_docs/alpha", watch: true },
      beta: { path: "./knowledge_docs/beta", watch: false },
    });
    setKnowledgeApiMock({});

    render(<Knowledge />);

    await screen.findByText("Knowledge Bases");

    expect(screen.queryByText(/Active:/)).not.toBeInTheDocument();
    expect(
      screen.getByText("Select a knowledge base to view and manage files."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Delete Active Base" }),
    ).toBeDisabled();
    expect(vi.mocked(global.fetch)).not.toHaveBeenCalled();
  });

  it("auto-selects and loads the only configured base", async () => {
    mockStore({
      docs: { path: "./knowledge_docs/docs", watch: true },
    });
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 1,
          indexed_count: 1,
        },
        files: {
          base_id: "docs",
          files: [
            {
              name: "intro.md",
              path: "intro.md",
              size: 123,
              modified: "2026-02-09T00:00:00.000Z",
              type: "md",
            },
          ],
          total_size: 123,
          file_count: 1,
        },
      },
    });

    render(<Knowledge />);

    await screen.findByText("Knowledge Bases");

    await waitFor(() => {
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        1,
        API_ENDPOINTS.knowledge.status("docs"),
        undefined,
      );
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        2,
        API_ENDPOINTS.knowledge.files("docs"),
        undefined,
      );
    });
    expect(screen.getByText("Active: docs")).toBeInTheDocument();
  });

  it("loads the selected base when a base card is clicked", async () => {
    mockStore({
      alpha: { path: "./knowledge_docs/alpha", watch: true },
      beta: { path: "./knowledge_docs/beta", watch: false },
    });
    setKnowledgeApiMock({
      beta: {
        status: {
          base_id: "beta",
          folder_path: "./knowledge_docs/beta",
          watch: false,
          file_count: 2,
          indexed_count: 2,
        },
        files: {
          base_id: "beta",
          files: [
            {
              name: "a.txt",
              path: "a.txt",
              size: 10,
              modified: "2026-02-09T00:00:00.000Z",
              type: "txt",
            },
            {
              name: "b.txt",
              path: "b.txt",
              size: 20,
              modified: "2026-02-09T00:01:00.000Z",
              type: "txt",
            },
          ],
          total_size: 30,
          file_count: 2,
        },
      },
    });

    render(<Knowledge />);

    await screen.findByText("Knowledge Bases");
    expect(vi.mocked(global.fetch)).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /beta/i }));

    await waitFor(() => {
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        1,
        API_ENDPOINTS.knowledge.status("beta"),
        undefined,
      );
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        2,
        API_ENDPOINTS.knowledge.files("beta"),
        undefined,
      );
    });
    expect(screen.getByText("Active: beta")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Delete Active Base" }),
    ).not.toBeDisabled();
  });

  it("ignores stale status and file responses after the selected base changes", async () => {
    let knowledgeBases: Record<string, KnowledgeBaseConfig> = {
      alpha: { path: "./knowledge_docs/alpha", watch: true },
    };
    const storeMock = useConfigStore as unknown as Mock;
    storeMock.mockImplementation(() => ({
      config: {
        knowledge_bases: knowledgeBases,
      } as unknown as Config,
      updateKnowledgeBase: mockUpdateKnowledgeBase,
      deleteKnowledgeBase: mockDeleteKnowledgeBase,
      saveConfig: mockSaveConfig,
      isDirty: false,
    }));

    const alphaStatus = deferredJsonResponse({
      base_id: "alpha",
      folder_path: "./knowledge_docs/alpha",
      watch: true,
      file_count: 1,
      indexed_count: 1,
    });
    const alphaFiles = deferredJsonResponse({
      base_id: "alpha",
      files: [
        {
          name: "alpha.md",
          path: "alpha.md",
          size: 10,
          modified: "2026-02-09T00:00:00.000Z",
          type: "md",
        },
      ],
      total_size: 10,
      file_count: 1,
    });
    const betaStatus = deferredJsonResponse({
      base_id: "beta",
      folder_path: "./knowledge_docs/beta",
      watch: false,
      file_count: 1,
      indexed_count: 1,
    });
    const betaFiles = deferredJsonResponse({
      base_id: "beta",
      files: [
        {
          name: "beta.md",
          path: "beta.md",
          size: 20,
          modified: "2026-02-09T00:01:00.000Z",
          type: "md",
        },
      ],
      total_size: 20,
      file_count: 1,
    });

    vi.mocked(global.fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === API_ENDPOINTS.knowledge.status("alpha")) {
        return alphaStatus.promise;
      }
      if (url === API_ENDPOINTS.knowledge.files("alpha")) {
        return alphaFiles.promise;
      }
      if (url === API_ENDPOINTS.knowledge.status("beta")) {
        return betaStatus.promise;
      }
      if (url === API_ENDPOINTS.knowledge.files("beta")) {
        return betaFiles.promise;
      }
      return Promise.resolve(
        jsonResponse({ detail: `Unhandled URL: ${url}` }, 404),
      );
    });

    const { rerender } = render(<Knowledge />);

    await waitFor(() => {
      expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
        API_ENDPOINTS.knowledge.status("alpha"),
        undefined,
      );
      expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
        API_ENDPOINTS.knowledge.files("alpha"),
        undefined,
      );
    });

    knowledgeBases = {
      beta: { path: "./knowledge_docs/beta", watch: false },
    };
    rerender(<Knowledge />);

    await waitFor(() => {
      expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
        API_ENDPOINTS.knowledge.status("beta"),
        undefined,
      );
      expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
        API_ENDPOINTS.knowledge.files("beta"),
        undefined,
      );
    });

    betaStatus.resolve();
    betaFiles.resolve();
    await screen.findByText("Active: beta");
    expect(screen.getAllByText("beta.md")).not.toHaveLength(0);

    alphaStatus.resolve();
    alphaFiles.resolve();
    await waitFor(() => {
      expect(screen.getByText("Active: beta")).toBeInTheDocument();
      expect(screen.getAllByText("beta.md")).not.toHaveLength(0);
      expect(screen.queryByText("alpha.md")).not.toBeInTheDocument();
    });
  });

  it("creates a git-based knowledge base in one step", async () => {
    mockStore({});
    setKnowledgeApiMock({
      docs_git: {
        status: {
          base_id: "docs_git",
          folder_path: "./knowledge_docs/docs_git",
          watch: true,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs_git",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Knowledge Bases");

    fireEvent.change(screen.getByLabelText("Base Name"), {
      target: { value: "docs_git" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create git source" }));
    fireEvent.change(screen.getByLabelText("Repository URL"), {
      target: { value: "https://github.com/org/repo" },
    });
    fireEvent.change(screen.getByLabelText("Branch"), {
      target: { value: "develop" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Git Base" }));

    await waitFor(() => {
      expect(mockUpdateKnowledgeBase).toHaveBeenCalledWith(
        "docs_git",
        expect.objectContaining({
          path: "./knowledge_docs/docs_git",
          watch: true,
          chunk_size: 5000,
          chunk_overlap: 0,
          git: expect.objectContaining({
            repo_url: "https://github.com/org/repo",
            branch: "develop",
            poll_interval_seconds: 300,
            skip_hidden: true,
          }),
        }),
      );
      expect(mockSaveConfig).toHaveBeenCalledTimes(1);
    });
  });

  it("shows git badge and repo details on git knowledge base cards", async () => {
    mockStore({
      local_docs: { path: "./knowledge_docs/local_docs", watch: true },
      git_docs: {
        path: "./knowledge_docs/git_docs",
        watch: true,
        git: {
          repo_url: "https://token:secret@github.com/org/git-docs",
          branch: "release",
        },
      },
    });
    setKnowledgeApiMock({});

    render(<Knowledge />);
    await screen.findByText("Knowledge Bases");

    const gitCard = screen.getByRole("button", { name: /git_docs/i });
    expect(gitCard).toHaveTextContent("Git");
    expect(gitCard).toHaveTextContent("https://***@github.com/org/git-docs");
    expect(gitCard).not.toHaveTextContent("token:secret");
    expect(gitCard).toHaveTextContent("Branch: release");
  });

  it("does not render stored git URL credentials in the settings input", async () => {
    mockStore({
      git_docs: {
        path: "./knowledge_docs/git_docs",
        watch: true,
        git: {
          repo_url:
            "https://token:secret@github.com/org/git-docs;token=path-secret?token=query-secret#fragment-secret",
          branch: "release",
        },
      },
    });
    setKnowledgeApiMock({
      git_docs: {
        status: {
          base_id: "git_docs",
          folder_path: "./knowledge_docs/git_docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
          git: {
            repo_url:
              "https://token:secret@github.com/org/git-docs;token=path-secret?token=query-secret#fragment-secret",
            branch: "release",
            lfs: false,
            syncing: false,
            repo_present: true,
            initial_sync_complete: true,
            last_successful_sync_at: null,
            last_successful_commit: null,
            last_error: null,
          },
        },
        files: {
          base_id: "git_docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: git_docs");

    const repoInput = screen.getByLabelText(
      "Current Repository URL",
    ) as HTMLInputElement;
    expect(repoInput.value).toBe("https://***@github.com/org/git-docs");
    expect(repoInput.readOnly).toBe(true);
    expect(repoInput.value).not.toContain("secret");
    expect(repoInput.value).not.toContain("path-secret");
    expect(repoInput.value).not.toContain("query-secret");
    expect(repoInput.value).not.toContain("fragment-secret");
    expect(screen.queryByText(/path-secret/)).not.toBeInTheDocument();
    expect(screen.queryByText(/query-secret/)).not.toBeInTheDocument();
    expect(screen.queryByText(/fragment-secret/)).not.toBeInTheDocument();

    const replacementInput = screen.getByLabelText(
      "Replacement Repository URL",
    ) as HTMLInputElement;
    expect(replacementInput.value).toBe("");
  });

  it.each([
    {
      repoUrl: "https://github.com/org/git-docs?token=query-secret",
      secret: "query-secret",
    },
    {
      repoUrl: "https://github.com/org/git-docs#fragment-secret",
      secret: "fragment-secret",
    },
    {
      repoUrl:
        "https://token:password@github.com/org/git-docs?token=query-secret#fragment-secret",
      secret: "query-secret",
    },
    {
      repoUrl:
        "https://token:password@github.com/org/git-docs;token=path-secret?token=query-secret#fragment-secret",
      secret: "path-secret",
    },
  ])(
    "strips git repo URL path params, query, and fragment secrets",
    async ({ repoUrl, secret }) => {
      mockStore({
        git_docs: {
          path: "./knowledge_docs/git_docs",
          watch: true,
          git: {
            repo_url: repoUrl,
            branch: "release",
          },
        },
      });
      setKnowledgeApiMock({
        git_docs: {
          status: {
            base_id: "git_docs",
            folder_path: "./knowledge_docs/git_docs",
            watch: true,
            file_count: 0,
            indexed_count: 0,
            git: {
              repo_url: repoUrl,
              branch: "release",
              lfs: false,
              syncing: false,
              repo_present: true,
              initial_sync_complete: true,
              last_successful_sync_at: null,
              last_successful_commit: null,
              last_error: null,
            },
          },
          files: {
            base_id: "git_docs",
            files: [],
            total_size: 0,
            file_count: 0,
          },
        },
      });

      render(<Knowledge />);
      await screen.findByText("Active: git_docs");

      expect(
        screen.getAllByText(/https:\/\/.*github\.com\/org\/git-docs/),
      ).not.toHaveLength(0);
      expect(screen.queryByText(new RegExp(secret))).not.toBeInTheDocument();
      expect(screen.queryByText(/fragment-secret/)).not.toBeInTheDocument();
      expect(screen.queryByText(/token:password/)).not.toBeInTheDocument();
    },
  );

  it("describes watch false as requiring reindex for external edits", async () => {
    mockStore({
      docs: { path: "./knowledge_docs/docs", watch: false },
    });
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: false,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    expect(screen.getByText("External edits need reindex")).toBeInTheDocument();
    expect(screen.queryByText("Manual reindex only")).not.toBeInTheDocument();
  });

  it("shows non-git refresh activity from top-level status fields", async () => {
    mockStore({
      docs: { path: "./knowledge_docs/docs", watch: true },
    });
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 2,
          indexed_count: 1,
          refreshing: true,
          refresh_state: "refreshing",
          refresh_job: "running",
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    expect(screen.getByText("Refresh Running")).toBeInTheDocument();
  });

  it("shows non-git refresh failures with redacted last error", async () => {
    mockStore({
      docs: { path: "./knowledge_docs/docs", watch: true },
    });
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 2,
          indexed_count: 1,
          refreshing: false,
          refresh_state: "refresh_failed",
          refresh_job: "failed",
          last_error: "Git command failed: https://***@example.com/repo.git",
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    expect(screen.getByText("Refresh Failed")).toBeInTheDocument();
    expect(screen.getByText(/Refresh Error:/)).toHaveTextContent(
      "Git command failed: https://***@example.com/repo.git",
    );
    expect(screen.queryByText(/token:secret/)).not.toBeInTheDocument();
  });

  it("shows git sync status details from the API", async () => {
    mockStore({
      git_docs: {
        path: "./knowledge_docs/git_docs",
        watch: true,
        git: {
          repo_url: "https://github.com/org/git-docs",
          branch: "release",
        },
      },
    });
    setKnowledgeApiMock({
      git_docs: {
        status: {
          base_id: "git_docs",
          folder_path: "./knowledge_docs/git_docs",
          watch: true,
          file_count: 4,
          indexed_count: 3,
          git: {
            repo_url: "https://token:secret@github.com/org/git-docs",
            branch: "release",
            lfs: true,
            syncing: true,
            repo_present: true,
            initial_sync_complete: false,
            last_successful_sync_at: "2026-04-17T12:00:00+00:00",
            last_successful_commit: "abc123",
            last_error: "fetch failed",
          },
        },
        files: {
          base_id: "git_docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: git_docs");

    expect(
      screen.getByText("https://***@github.com/org/git-docs"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/token:secret/)).not.toBeInTheDocument();
    expect(screen.getByText("Refreshing")).toBeInTheDocument();
    expect(screen.getByText("Repo Present")).toBeInTheDocument();
    expect(screen.getByText("Snapshot Pending")).toBeInTheDocument();
    expect(screen.getByText("LFS")).toBeInTheDocument();
    expect(screen.getByText("Git Error")).toBeInTheDocument();
    expect(screen.getByText(/Last Commit:/)).toHaveTextContent("abc123");
    expect(screen.getByText(/Git Error:/)).toHaveTextContent("fetch failed");
  });

  it("shows degraded git file-listing details from the API", async () => {
    mockStore({
      git_docs: {
        path: "./knowledge_docs/git_docs",
        watch: true,
        git: {
          repo_url: "https://github.com/org/git-docs",
          branch: "release",
        },
      },
    });
    setKnowledgeApiMock({
      git_docs: {
        status: {
          base_id: "git_docs",
          folder_path: "./knowledge_docs/git_docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
          file_listing_degraded: true,
          file_listing_error:
            "Git command timed out after 0.01s: git ls-files -z",
          git: {
            repo_url: "https://github.com/org/git-docs",
            branch: "release",
            lfs: false,
            syncing: false,
            repo_present: true,
            initial_sync_complete: true,
            last_successful_sync_at: null,
            last_successful_commit: null,
            last_error: null,
          },
        },
        files: {
          base_id: "git_docs",
          files: [],
          total_size: 0,
          file_count: 0,
          file_listing_degraded: true,
          file_listing_error:
            "Git command timed out after 0.01s: git ls-files -z",
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: git_docs");

    expect(screen.getByText("File Listing Degraded")).toBeInTheDocument();
    expect(screen.getByText(/Git command timed out/)).toBeInTheDocument();
  });

  it("hides upload, drop, and file delete controls for git knowledge bases", async () => {
    mockStore({
      git_docs: {
        path: "./knowledge_docs/git_docs",
        watch: true,
        git: {
          repo_url: "https://github.com/org/git-docs",
          branch: "release",
        },
      },
    });
    setKnowledgeApiMock({
      git_docs: {
        status: {
          base_id: "git_docs",
          folder_path: "./knowledge_docs/git_docs",
          watch: true,
          file_count: 1,
          indexed_count: 1,
          git: {
            repo_url: "https://github.com/org/git-docs",
            branch: "release",
            lfs: false,
            syncing: false,
            repo_present: true,
            initial_sync_complete: true,
            last_successful_sync_at: null,
            last_successful_commit: "abc123",
            last_error: null,
          },
        },
        files: {
          base_id: "git_docs",
          files: [
            {
              name: "guide.md",
              path: "guide.md",
              size: 42,
              modified: "2026-02-09T00:00:00.000Z",
              type: "md",
            },
          ],
          total_size: 42,
          file_count: 1,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: git_docs");

    expect(screen.getByText("Repository-managed files")).toBeInTheDocument();
    expect(
      screen.queryByText("Drop files here or upload manually"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Upload" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete guide.md" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reindex" })).toBeInTheDocument();
  });

  it("does not upload dropped files while settings are dirty", async () => {
    mockStore(
      {
        docs: { path: "./knowledge_docs/docs", watch: true },
      },
      { isDirty: true },
    );
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    const droppedFile = new File(["hello"], "guide.md", {
      type: "text/markdown",
    });
    const uploadTarget = screen.getByText("Drop files here or upload manually");
    fireEvent.dragOver(uploadTarget, {
      dataTransfer: { files: [droppedFile] },
    });
    fireEvent.drop(uploadTarget, {
      dataTransfer: { files: [droppedFile] },
    });

    const uploadCalls = vi
      .mocked(global.fetch)
      .mock.calls.filter(([input]) => String(input).includes("/upload"));
    expect(uploadCalls).toHaveLength(0);
  });

  it("surfaces structured reindex failure details", async () => {
    mockStore({
      git_docs: {
        path: "./knowledge_docs/git_docs",
        watch: true,
        git: {
          repo_url: "https://github.com/org/git-docs",
          branch: "release",
        },
      },
    });
    setKnowledgeApiMock(
      {
        git_docs: {
          status: {
            base_id: "git_docs",
            folder_path: "./knowledge_docs/git_docs",
            watch: true,
            file_count: 1,
            indexed_count: 0,
            git: {
              repo_url: "https://github.com/org/git-docs",
              branch: "release",
              lfs: false,
              syncing: false,
              repo_present: true,
              initial_sync_complete: false,
              last_successful_sync_at: null,
              last_successful_commit: null,
              last_error: null,
            },
          },
          files: {
            base_id: "git_docs",
            files: [],
            total_size: 0,
            file_count: 0,
          },
        },
      },
      {
        reindexResponses: {
          git_docs: jsonResponse(
            {
              detail: {
                success: false,
                base_id: "git_docs",
                indexed_count: 0,
                availability: "refresh_failed",
                last_error: "Indexed 0 of 1 managed knowledge files",
              },
            },
            409,
          ),
        },
      },
    );

    render(<Knowledge />);
    await screen.findByText("Active: git_docs");

    fireEvent.click(screen.getByRole("button", { name: "Reindex" }));

    expect(
      await screen.findByText("Indexed 0 of 1 managed knowledge files"),
    ).toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Reindex failed",
        description: "Indexed 0 of 1 managed knowledge files",
        variant: "destructive",
      }),
    );
  });

  it("switches source type in settings and toggles git fields", async () => {
    mockStore({
      docs: { path: "./knowledge_docs/docs", watch: true },
    });
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    expect(screen.getByLabelText("Folder Path")).toBeInTheDocument();
    expect(screen.getByText("Refresh on Access")).toBeInTheDocument();
    expect(screen.getByText(/Local folders only/)).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Current Repository URL"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Replacement Repository URL"),
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Settings git source" }),
    );

    expect(screen.getByLabelText("Current Repository URL")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Replacement Repository URL"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Folder Path")).toBeInTheDocument();
    expect(screen.queryByText("Refresh on Access")).not.toBeInTheDocument();
    expect(mockUpdateKnowledgeBase).toHaveBeenCalledWith(
      "docs",
      expect.objectContaining({
        git: expect.objectContaining({
          repo_url: "",
          branch: "main",
          poll_interval_seconds: 300,
          skip_hidden: true,
        }),
      }),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Settings local source" }),
    );

    expect(
      screen.queryByLabelText("Current Repository URL"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Replacement Repository URL"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Refresh on Access")).toBeInTheDocument();
    expect(mockUpdateKnowledgeBase).toHaveBeenLastCalledWith(
      "docs",
      expect.objectContaining({ git: undefined }),
    );
  });

  it("saves updated git settings from base settings", async () => {
    mockStore(
      {
        docs: {
          path: "./knowledge_docs/docs",
          watch: true,
          chunk_size: 5000,
          chunk_overlap: 0,
          git: {
            repo_url: "https://github.com/org/repo",
            branch: "main",
          },
        },
      },
      { isDirty: true },
    );
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    expect(
      screen.getByText(
        "Minimum snapshot age before checking for Git updates on access.",
      ),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Replacement Repository URL"), {
      target: { value: "  https://github.com/org/repo-updated  " },
    });
    fireEvent.change(screen.getByLabelText("Branch"), {
      target: { value: "  release  " },
    });
    fireEvent.change(screen.getByLabelText("Poll Interval (seconds)"), {
      target: { value: "45" },
    });
    fireEvent.change(screen.getByLabelText("Chunk Size (characters)"), {
      target: { value: "2048" },
    });
    fireEvent.change(screen.getByLabelText("Chunk Overlap (characters)"), {
      target: { value: "256" },
    });
    fireEvent.change(screen.getByLabelText("Credentials Service (optional)"), {
      target: { value: "  github-private  " },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Skip Hidden Files" }),
    );
    fireEvent.change(screen.getByLabelText("Include Patterns (optional)"), {
      target: { value: "docs/**\nknowledge/**" },
    });
    fireEvent.change(screen.getByLabelText("Exclude Patterns (optional)"), {
      target: { value: "docs/private/**" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalledTimes(1);
      expect(mockUpdateKnowledgeBase).toHaveBeenLastCalledWith(
        "docs",
        expect.objectContaining({
          chunk_size: 2048,
          chunk_overlap: 256,
          git: expect.objectContaining({
            repo_url: "https://github.com/org/repo-updated",
            branch: "release",
            poll_interval_seconds: 45,
            credentials_service: "github-private",
            lfs: false,
            sync_timeout_seconds: 3600,
            skip_hidden: false,
            include_patterns: ["docs/**", "knowledge/**"],
            exclude_patterns: ["docs/private/**"],
          }),
        }),
      );
    });
  });

  it("preserves a credential-bearing git URL when saving other git settings", async () => {
    mockStore(
      {
        docs: {
          path: "./knowledge_docs/docs",
          watch: true,
          chunk_size: 5000,
          chunk_overlap: 0,
          git: {
            repo_url: "https://token:secret@github.com/org/repo",
            branch: "main",
          },
        },
      },
      { isDirty: true },
    );
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.change(screen.getByLabelText("Branch"), {
      target: { value: "  release  " },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalledTimes(1);
      expect(mockUpdateKnowledgeBase).toHaveBeenLastCalledWith(
        "docs",
        expect.objectContaining({
          git: expect.objectContaining({
            repo_url: "https://token:secret@github.com/org/repo",
            branch: "release",
          }),
        }),
      );
    });
  });

  it("rejects git URL replacements containing the redaction sentinel", async () => {
    mockStore({
      docs: {
        path: "./knowledge_docs/docs",
        watch: true,
        chunk_size: 5000,
        chunk_overlap: 0,
        git: {
          repo_url: "https://github.com/org/repo",
          branch: "main",
        },
      },
    });
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.change(screen.getByLabelText("Replacement Repository URL"), {
      target: { value: "https://***@github.com/org/repo" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    expect(
      await screen.findByText(
        "Repository URL contains a redacted credential placeholder",
      ),
    ).toBeInTheDocument();
    expect(mockSaveConfig).not.toHaveBeenCalled();
    expect(mockUpdateKnowledgeBase).not.toHaveBeenCalled();
  });

  it("saves advanced git settings from base settings", async () => {
    mockStore(
      {
        docs: {
          path: "./knowledge_docs/docs",
          watch: true,
          chunk_size: 5000,
          chunk_overlap: 0,
          git: {
            repo_url: "https://github.com/org/repo",
            branch: "main",
            lfs: true,
            sync_timeout_seconds: 1800,
          },
        },
      },
      { isDirty: true },
    );
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: "docs",
          folder_path: "./knowledge_docs/docs",
          watch: true,
          file_count: 0,
          indexed_count: 0,
        },
        files: {
          base_id: "docs",
          files: [],
          total_size: 0,
          file_count: 0,
        },
      },
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.click(screen.getByRole("checkbox", { name: "Enable Git LFS" }));
    fireEvent.change(screen.getByLabelText("Sync Timeout (seconds)"), {
      target: { value: "900" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));
    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalledTimes(1);
      expect(mockUpdateKnowledgeBase).toHaveBeenLastCalledWith(
        "docs",
        expect.objectContaining({
          git: expect.objectContaining({
            repo_url: "https://github.com/org/repo",
            branch: "main",
            lfs: false,
            sync_timeout_seconds: 900,
          }),
        }),
      );
    });
  });
});
