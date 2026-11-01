import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { Knowledge } from "./Knowledge";
import { API_ENDPOINTS } from "@/lib/api";
import { useConfigStore } from "@/store/configStore";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import { confirmLeavingUnsavedDrafts } from "@/hooks/useUnsavedDraft";
import type { SaveConfigResult } from "@/store/configStore";
import type { Config, KnowledgeBaseConfig } from "@/types/config";

vi.mock("@/store/configStore", () => ({
  useConfigStore: vi.fn(),
}));
vi.mock("@/hooks/useConfigSchema", () => ({
  useConfigSchema: vi.fn(() => ({ schema: null, error: null, retry: vi.fn() })),
}));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockUpdateKnowledgeBase = vi.fn();
const mockUpdateConfigValue = vi.fn();
const mockDeleteKnowledgeBase = vi.fn();
const mockSaveConfig = vi
  .fn<() => Promise<SaveConfigResult>>()
  .mockResolvedValue({ status: "saved" });

type KnowledgeApiPayloads = {
  status: {
    base_id: string;
    description?: string;
    folder_path: string;
    watch: boolean;
    file_count: number;
    mode?: "semantic" | "files";
    indexed_count: number;
    refreshing?: boolean;
    refresh_state?: "none" | "stale" | "refreshing" | "refresh_failed";
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

function emptyKnowledgePayloads(
  baseId: string,
  status: Partial<KnowledgeApiPayloads["status"]> = {},
): KnowledgeApiPayloads {
  return {
    status: {
      base_id: baseId,
      folder_path: `./knowledge_docs/${baseId}`,
      watch: true,
      file_count: 0,
      indexed_count: 0,
      ...status,
    },
    files: { base_id: baseId, files: [], total_size: 0, file_count: 0 },
  };
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
    agents: [],
    rooms: [],
    diagnostics: [],
    updateKnowledgeBase: mockUpdateKnowledgeBase,
    updateConfigValue: mockUpdateConfigValue,
    deleteKnowledgeBase: mockDeleteKnowledgeBase,
    saveConfig: mockSaveConfig,
    isDirty: options.isDirty ?? false,
  });
}

/** Back the component with the real store so edits re-render, while recording writes and stubbing saves. */
async function mockStatefulStore(
  knowledgeBases: Record<string, KnowledgeBaseConfig>,
) {
  const actual = await vi.importActual<typeof import("@/store/configStore")>(
    "@/store/configStore",
  );
  const config = { knowledge_bases: knowledgeBases } as unknown as Config;
  actual.useConfigStore.setState({
    config,
    loadedConfig: config,
    isDirty: false,
  });
  (useConfigStore as unknown as Mock).mockImplementation(() => ({
    ...actual.useConfigStore(),
    updateKnowledgeBase: (
      baseName: string,
      baseConfig: KnowledgeBaseConfig,
    ) => {
      mockUpdateKnowledgeBase(baseName, baseConfig);
      actual.useConfigStore
        .getState()
        .updateKnowledgeBase(baseName, baseConfig);
    },
    saveConfig: mockSaveConfig,
  }));
  return actual.useConfigStore;
}

describe("Knowledge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: null,
      error: null,
      retry: vi.fn(),
    });
  });

  it("edits knowledge base filters through More settings", async () => {
    mockStore({ docs: { path: "./knowledge_docs/docs", watch: true } });
    setKnowledgeApiMock({});
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: {},
        $defs: {
          KnowledgeBaseConfig: {
            type: "object",
            properties: {
              path: { type: "string" },
              exclude_extensions: {
                type: "array",
                items: { type: "string" },
                description: "Extensions to skip",
              },
            },
          },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<Knowledge />);
    fireEvent.click(
      await screen.findByRole("button", { name: /More settings/ }),
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "New exclude extensions entry" }),
      { target: { value: ".log" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Add exclude extensions entry" }),
    );

    expect(mockUpdateConfigValue).toHaveBeenCalledWith(
      ["knowledge_bases", "docs", "exclude_extensions"],
      [".log"],
    );
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

  it("asks before leaving the page drops a half-filled Create form", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    mockStore({});
    setKnowledgeApiMock({});
    render(<Knowledge />);
    await screen.findByText("Knowledge Bases");
    fireEvent.click(screen.getByRole("button", { name: "Create git source" }));
    expect(confirmLeavingUnsavedDrafts()).toBe(true);
    expect(confirmSpy).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Base Name"), {
      target: { value: "docs_git" },
    });

    expect(confirmLeavingUnsavedDrafts()).toBe(false);
    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
    confirmSpy.mockRestore();
  });

  it("counts a changed Git branch in the Create form as unsaved until it is restored", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    mockStore({});
    setKnowledgeApiMock({});
    render(<Knowledge />);
    await screen.findByText("Knowledge Bases");
    fireEvent.click(screen.getByRole("button", { name: "Create git source" }));

    fireEvent.change(screen.getByLabelText("Branch"), {
      target: { value: "release" },
    });
    expect(confirmLeavingUnsavedDrafts()).toBe(false);

    fireEvent.change(screen.getByLabelText("Branch"), {
      target: { value: "main" },
    });
    confirmSpy.mockClear();
    expect(confirmLeavingUnsavedDrafts()).toBe(true);
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
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

  it("creates a knowledge base with a description", async () => {
    mockStore({});
    setKnowledgeApiMock({
      product_docs: emptyKnowledgePayloads("product_docs"),
    });

    render(<Knowledge />);
    await screen.findByText("Knowledge Bases");

    fireEvent.change(screen.getByLabelText("Base Name"), {
      target: { value: "product_docs" },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: {
        value:
          "Product requirements, roadmap notes, and user-facing decisions.",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add Base" }));

    await waitFor(() => {
      expect(mockUpdateKnowledgeBase).toHaveBeenCalledWith(
        "product_docs",
        expect.objectContaining({
          description:
            "Product requirements, roadmap notes, and user-facing decisions.",
        }),
      );
    });
  });

  it("creates a files-only knowledge base", async () => {
    mockStore({});
    setKnowledgeApiMock({
      file_docs: emptyKnowledgePayloads("file_docs", {
        folder_path:
          "${MINDROOM_STORAGE_PATH}/agents/helper/workspace/knowledge/file_docs",
        watch: false,
        mode: "files",
      }),
    });

    render(<Knowledge />);
    await screen.findByText("Knowledge Bases");

    fireEvent.change(screen.getByLabelText("Base Name"), {
      target: { value: "file_docs" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Create files-only access" }),
    );
    fireEvent.change(screen.getByLabelText("Folder Path"), {
      target: {
        value:
          "${MINDROOM_STORAGE_PATH}/agents/helper/workspace/knowledge/file_docs",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add Base" }));

    await waitFor(() => {
      expect(mockUpdateKnowledgeBase).toHaveBeenCalledWith(
        "file_docs",
        expect.objectContaining({
          mode: "files",
          path: "${MINDROOM_STORAGE_PATH}/agents/helper/workspace/knowledge/file_docs",
          watch: false,
        }),
      );
    });
  });

  it("requires an explicit files-only folder path when creating a base", async () => {
    mockStore({});
    setKnowledgeApiMock({});

    render(<Knowledge />);
    await screen.findByText("Knowledge Bases");

    fireEvent.change(screen.getByLabelText("Base Name"), {
      target: { value: "file_docs" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Create files-only access" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add Base" }));

    expect(
      screen.getByText(
        "Folder path is required for files-only knowledge bases",
      ),
    ).toBeInTheDocument();
    expect(mockUpdateKnowledgeBase).not.toHaveBeenCalled();
    expect(mockSaveConfig).not.toHaveBeenCalled();
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

  it("keeps scp-style git repo URLs visible in git knowledge UI", async () => {
    mockStore({
      git_docs: {
        path: "./knowledge_docs/git_docs",
        watch: true,
        git: {
          repo_url: "git@github.com:org/git-docs.git",
          branch: "release",
        },
      },
    });
    setKnowledgeApiMock({
      git_docs: emptyKnowledgePayloads("git_docs", {
        git: {
          repo_url: "git@github.com:org/git-docs.git",
          branch: "release",
          lfs: false,
          syncing: false,
          repo_present: true,
          initial_sync_complete: true,
          last_successful_sync_at: null,
          last_successful_commit: null,
          last_error: null,
        },
      }),
    });

    render(<Knowledge />);
    await screen.findByText("Active: git_docs");

    const gitCard = screen.getByRole("button", { name: /git_docs/i });
    expect(gitCard).toHaveTextContent("git@github.com:org/git-docs.git");
    expect(
      (screen.getByLabelText("Current Repository URL") as HTMLInputElement)
        .value,
    ).toBe("git@github.com:org/git-docs.git");
    expect(
      screen.getAllByText("git@github.com:org/git-docs.git").length,
    ).toBeGreaterThan(0);
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
      git_docs: emptyKnowledgePayloads("git_docs", {
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
      }),
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
        git_docs: emptyKnowledgePayloads("git_docs", {
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
        }),
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
      docs: emptyKnowledgePayloads("docs", {
        watch: false,
      }),
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
      docs: emptyKnowledgePayloads("docs", {
        file_count: 2,
        indexed_count: 1,
        refreshing: true,
        refresh_state: "refreshing",
      }),
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
      docs: emptyKnowledgePayloads("docs", {
        file_count: 2,
        indexed_count: 1,
        refreshing: false,
        refresh_state: "refresh_failed",
        last_error: "Git command failed: https://***@example.com/repo.git",
      }),
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
      git_docs: emptyKnowledgePayloads("git_docs", {
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
      }),
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

  it("labels Git files-only refresh actions as sync", async () => {
    mockStore({
      git_docs: {
        mode: "files",
        path: "${MINDROOM_STORAGE_PATH}/agents/helper/workspace/knowledge/git_docs",
        watch: false,
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
          folder_path:
            "${MINDROOM_STORAGE_PATH}/agents/helper/workspace/knowledge/git_docs",
          watch: false,
          mode: "files",
          file_count: 1,
          indexed_count: 0,
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

    fireEvent.click(screen.getByRole("button", { name: "Sync" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Sync complete",
          description: "Knowledge base 'git_docs' synced successfully.",
        }),
      );
    });
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

  it.each([
    {
      body: "structured details",
      response: jsonResponse(
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
      message: "Indexed 0 of 1 managed knowledge files",
    },
    {
      body: "no JSON",
      response: new Response("<html>Bad gateway</html>", { status: 502 }),
      message: "Request failed (502)",
    },
    {
      body: "no JSON and a status text",
      response: new Response("<html>Bad gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
      }),
      message: "Bad Gateway",
    },
  ])("surfaces a reindex failure with $body", async ({ response, message }) => {
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
        git_docs: emptyKnowledgePayloads("git_docs", {
          file_count: 1,
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
        }),
      },
      { reindexResponses: { git_docs: response } },
    );

    render(<Knowledge />);
    await screen.findByText("Active: git_docs");

    fireEvent.click(screen.getByRole("button", { name: "Reindex" }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Reindex failed",
        description: message,
        variant: "destructive",
      }),
    );
  });

  it("switches source type in settings and toggles git fields", async () => {
    await mockStatefulStore({
      docs: { path: "./knowledge_docs/docs", watch: true },
    });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs"),
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

  it("drops the local poll interval from the payload when a polled base switches to git", async () => {
    const polledBase: KnowledgeBaseConfig = {
      path: "./knowledge_docs/docs",
      watch: true,
      poll_interval_seconds: 600,
    };
    mockStore({ docs: polledBase });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs"),
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.click(
      screen.getByRole("button", { name: "Settings git source" }),
    );

    const update = mockUpdateKnowledgeBase.mock.lastCall?.[1] as
      | KnowledgeBaseConfig
      | undefined;
    expect(update?.git).toBeDefined();
    // The store merges updates into the existing base, then JSON.stringify drops undefined keys.
    const savedBase = JSON.parse(
      JSON.stringify({ ...polledBase, ...update }),
    ) as Record<string, unknown>;
    expect(savedBase).not.toHaveProperty("poll_interval_seconds");
    expect(savedBase).toHaveProperty("git");
  });

  it("hides local refresh controls for files-only bases", async () => {
    mockStore({
      docs: {
        mode: "files",
        path: "${MINDROOM_STORAGE_PATH}/agents/helper/workspace/knowledge/docs",
        watch: false,
      },
    });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs", {
        folder_path:
          "${MINDROOM_STORAGE_PATH}/agents/helper/workspace/knowledge/docs",
        watch: false,
        mode: "files",
      }),
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    expect(screen.queryByText("Refresh on Access")).not.toBeInTheDocument();
  });

  it("clears the default semantic path when switching settings to files-only", async () => {
    mockStore({
      docs: {
        path: "./knowledge_docs/docs",
        watch: true,
      },
    });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs"),
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.click(
      screen.getByRole("button", { name: "Settings files-only access" }),
    );

    expect(mockUpdateKnowledgeBase).toHaveBeenCalledWith(
      "docs",
      expect.objectContaining({
        mode: "files",
        path: "",
        watch: false,
        poll_interval_seconds: undefined,
      }),
    );
  });

  it("updates a knowledge base description from base settings", async () => {
    mockStore({
      docs: {
        description: "Old docs description",
        path: "./knowledge_docs/docs",
        watch: true,
      },
    });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs", {
        description: "Old docs description",
      }),
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.change(screen.getByLabelText("Search Description"), {
      target: {
        value: "Updated docs description for the model-facing search tool.",
      },
    });

    expect(mockUpdateKnowledgeBase).toHaveBeenCalledWith(
      "docs",
      expect.objectContaining({
        description:
          "Updated docs description for the model-facing search tool.",
      }),
    );
  });

  it("writes a description edit through the store and shows the store value", async () => {
    const store = await mockStatefulStore({
      docs: {
        description: "Old docs description",
        path: "./knowledge_docs/docs",
        watch: true,
        chunk_size: 64,
      },
    });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs", {
        description: "Old docs description",
      }),
    });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    expect(screen.getByLabelText("Chunk Size (characters)")).toHaveValue(128);

    const descriptionInput = screen.getByLabelText("Search Description");
    fireEvent.change(descriptionInput, {
      target: { value: "New docs description" },
    });

    expect(descriptionInput).toHaveValue("New docs description");
    expect(store.getState().config?.knowledge_bases?.docs).toEqual({
      description: "New docs description",
      mode: "semantic",
      path: "./knowledge_docs/docs",
      watch: true,
      chunk_size: 128,
      chunk_overlap: 0,
      git: undefined,
    });
    expect(store.getState().isDirty).toBe(true);
  });

  it("keeps a chunk size typed key by key and shows the stored value on blur", async () => {
    const store = await mockStatefulStore({
      docs: { path: "./knowledge_docs/docs", watch: true },
    });
    setKnowledgeApiMock({ docs: emptyKnowledgePayloads("docs") });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    const chunkSize = screen.getByLabelText("Chunk Size (characters)");
    for (const value of ["", "2", "20", "204", "2048"]) {
      fireEvent.change(chunkSize, { target: { value } });
      expect(chunkSize).toHaveDisplayValue(value);
    }
    expect(store.getState().config?.knowledge_bases?.docs?.chunk_size).toBe(
      2048,
    );

    fireEvent.change(chunkSize, { target: { value: "2" } });
    fireEvent.blur(chunkSize);
    expect(chunkSize).toHaveDisplayValue("128");
  });

  it("shows the chunk overlap a save would write while the chunk size is retyped", async () => {
    const store = await mockStatefulStore({
      docs: {
        path: "./knowledge_docs/docs",
        watch: true,
        chunk_size: 1000,
        chunk_overlap: 300,
      },
    });
    setKnowledgeApiMock({ docs: emptyKnowledgePayloads("docs") });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    const chunkSize = screen.getByLabelText("Chunk Size (characters)");
    for (const value of ["5", "50", "500"]) {
      fireEvent.change(chunkSize, { target: { value } });
    }
    const storedOverlap =
      store.getState().config?.knowledge_bases?.docs?.chunk_overlap;
    expect(
      screen.getByLabelText("Chunk Overlap (characters)"),
    ).toHaveDisplayValue(String(storedOverlap));
  });

  it("stores the chunk overlap it shows when one edit makes the size smaller", async () => {
    const store = await mockStatefulStore({
      docs: {
        path: "./knowledge_docs/docs",
        watch: true,
        chunk_size: 1000,
        chunk_overlap: 300,
      },
    });
    setKnowledgeApiMock({ docs: emptyKnowledgePayloads("docs") });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.change(screen.getByLabelText("Chunk Size (characters)"), {
      target: { value: "256" },
    });
    expect(
      screen.getByLabelText("Chunk Overlap (characters)"),
    ).toHaveDisplayValue("255");
    expect(store.getState().config?.knowledge_bases?.docs).toMatchObject({
      chunk_size: 256,
      chunk_overlap: 255,
    });
  });

  it("keeps a new line typed into the git include patterns", async () => {
    const store = await mockStatefulStore({
      docs: {
        path: "./knowledge_docs/docs",
        watch: true,
        git: { repo_url: "https://github.com/org/repo", branch: "main" },
      },
    });
    setKnowledgeApiMock({ docs: emptyKnowledgePayloads("docs") });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    const include = screen.getByLabelText("Include Patterns (optional)");
    for (const value of ["docs/**", "docs/**\n", "docs/**\nguides/**"]) {
      fireEvent.change(include, { target: { value } });
      expect(include).toHaveDisplayValue(value);
    }
    expect(
      store.getState().config?.knowledge_bases?.docs?.git?.include_patterns,
    ).toEqual(["docs/**", "guides/**"]);
  });

  it("keeps git settings typed key by key", async () => {
    const store = await mockStatefulStore({
      docs: {
        path: "./knowledge_docs/docs",
        watch: true,
        git: { repo_url: "https://github.com/org/repo", branch: "main" },
      },
    });
    setKnowledgeApiMock({ docs: emptyKnowledgePayloads("docs") });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    const branch = screen.getByLabelText("Branch");
    fireEvent.change(branch, { target: { value: "" } });
    expect(store.getState().config?.knowledge_bases?.docs?.git?.branch).toBe(
      "main",
    );
    for (const value of ["", "d", "dev"]) {
      fireEvent.change(branch, { target: { value } });
      expect(branch).toHaveDisplayValue(value);
    }
    const pollInterval = screen.getByLabelText("Poll Interval (seconds)");
    const syncTimeout = screen.getByLabelText("Sync Timeout (seconds)");
    for (const input of [pollInterval, syncTimeout]) {
      for (const value of ["", "3", "30"]) {
        fireEvent.change(input, { target: { value } });
        expect(input).toHaveDisplayValue(value);
      }
    }
    fireEvent.change(pollInterval, { target: { value: "3" } });
    expect(
      store.getState().config?.knowledge_bases?.docs?.git
        ?.poll_interval_seconds,
    ).toBe(5);
    fireEvent.change(pollInterval, { target: { value: "30" } });
    expect(store.getState().config?.knowledge_bases?.docs?.git).toMatchObject({
      branch: "dev",
      poll_interval_seconds: 30,
      sync_timeout_seconds: 30,
    });
  });

  it("stores a trimmed git branch and credentials service while keeping the typed text", async () => {
    const store = await mockStatefulStore({
      docs: {
        path: "./knowledge_docs/docs",
        watch: true,
        git: { repo_url: "https://github.com/org/repo", branch: "release" },
      },
    });
    setKnowledgeApiMock({ docs: emptyKnowledgePayloads("docs") });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    const branch = screen.getByLabelText("Branch");
    fireEvent.change(branch, { target: { value: "main " } });
    const credentialsService = screen.getByLabelText(
      "Credentials Service (optional)",
    );
    fireEvent.change(credentialsService, { target: { value: " github-pat " } });

    expect(branch).toHaveDisplayValue("main ");
    expect(credentialsService).toHaveDisplayValue(" github-pat ");
    // Every Save button sends this draft, not only Save Settings.
    expect(store.getState().config?.knowledge_bases?.docs?.git).toMatchObject({
      branch: "main",
      credentials_service: "github-pat",
    });

    fireEvent.change(credentialsService, { target: { value: " " } });
    expect(
      store.getState().config?.knowledge_bases?.docs?.git?.credentials_service,
    ).toBeUndefined();
  });

  it("saves updated git settings from base settings", async () => {
    await mockStatefulStore({
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
      docs: emptyKnowledgePayloads("docs"),
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
    await mockStatefulStore({
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
    });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs"),
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

  it("saves files-only bases without watch and reports superseded saves", async () => {
    mockStore(
      {
        docs: { mode: "files", path: "/srv/docs", watch: true },
      },
      { isDirty: true },
    );
    setKnowledgeApiMock({ docs: emptyKnowledgePayloads("docs") });
    mockSaveConfig.mockResolvedValueOnce({ status: "stale" });

    render(<Knowledge />);
    await screen.findByText("Active: docs");

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    expect(
      await screen.findByText("Save was superseded by newer draft edits"),
    ).toBeInTheDocument();
    expect(mockUpdateKnowledgeBase).toHaveBeenLastCalledWith(
      "docs",
      expect.objectContaining({ mode: "files", watch: false }),
    );
    expect(mockToast).toHaveBeenCalledWith({
      title: "Save failed",
      description: "Save was superseded by newer draft edits",
      variant: "destructive",
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
      docs: emptyKnowledgePayloads("docs"),
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
    await mockStatefulStore({
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
    });
    setKnowledgeApiMock({
      docs: emptyKnowledgePayloads("docs"),
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
