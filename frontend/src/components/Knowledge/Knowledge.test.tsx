import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { Knowledge } from './Knowledge';
import { API_ENDPOINTS } from '@/lib/api';
import { useConfigStore } from '@/store/configStore';
import type { Config, KnowledgeBaseConfig } from '@/types/config';

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockUpdateKnowledgeBase = vi.fn();
const mockDeleteKnowledgeBase = vi.fn();
const mockSaveConfig = vi.fn().mockResolvedValue(undefined);

type KnowledgeApiPayloads = {
  status: {
    base_id: string;
    folder_path: string;
    watch: boolean;
    file_count: number;
    indexed_count: number;
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
  };
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function setKnowledgeApiMock(payloadByBase: Record<string, KnowledgeApiPayloads>) {
  const fetchMock = vi.mocked(global.fetch);
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);

    const statusMatch = url.match(/\/api\/knowledge\/bases\/([^/]+)\/status$/);
    if (statusMatch) {
      const baseId = decodeURIComponent(statusMatch[1] ?? '');
      const payload = payloadByBase[baseId]?.status;
      return Promise.resolve(
        payload ? jsonResponse(payload) : jsonResponse({ detail: 'Not found' }, 404)
      );
    }

    const filesMatch = url.match(/\/api\/knowledge\/bases\/([^/]+)\/files$/);
    if (filesMatch) {
      const baseId = decodeURIComponent(filesMatch[1] ?? '');
      const payload = payloadByBase[baseId]?.files;
      return Promise.resolve(
        payload ? jsonResponse(payload) : jsonResponse({ detail: 'Not found' }, 404)
      );
    }

    return Promise.resolve(jsonResponse({ detail: `Unhandled URL: ${url}` }, 404));
  });
}

function mockStore(knowledgeBases: Record<string, KnowledgeBaseConfig>) {
  const storeMock = useConfigStore as unknown as Mock;
  storeMock.mockReturnValue({
    config: {
      knowledge_bases: knowledgeBases,
    } as unknown as Config,
    updateKnowledgeBase: mockUpdateKnowledgeBase,
    deleteKnowledgeBase: mockDeleteKnowledgeBase,
    saveConfig: mockSaveConfig,
    isDirty: false,
  });
}

describe('Knowledge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not auto-select the first base when multiple bases are configured', async () => {
    mockStore({
      alpha: { path: './knowledge_docs/alpha', watch: true },
      beta: { path: './knowledge_docs/beta', watch: false },
    });
    setKnowledgeApiMock({});

    render(<Knowledge />);

    await screen.findByText('Knowledge Bases');

    expect(screen.queryByText(/Active:/)).not.toBeInTheDocument();
    expect(
      screen.getByText('Select a knowledge base to view and manage files.')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Active Base' })).toBeDisabled();
    expect(vi.mocked(global.fetch)).not.toHaveBeenCalled();
  });

  it('auto-selects and loads the only configured base', async () => {
    mockStore({
      docs: { path: './knowledge_docs/docs', watch: true },
    });
    setKnowledgeApiMock({
      docs: {
        status: {
          base_id: 'docs',
          folder_path: './knowledge_docs/docs',
          watch: true,
          file_count: 1,
          indexed_count: 1,
        },
        files: {
          base_id: 'docs',
          files: [
            {
              name: 'intro.md',
              path: 'intro.md',
              size: 123,
              modified: '2026-02-09T00:00:00.000Z',
              type: 'md',
            },
          ],
          total_size: 123,
          file_count: 1,
        },
      },
    });

    render(<Knowledge />);

    await screen.findByText('Knowledge Bases');

    await waitFor(() => {
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        1,
        API_ENDPOINTS.knowledge.status('docs'),
        undefined
      );
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        2,
        API_ENDPOINTS.knowledge.files('docs'),
        undefined
      );
    });
    expect(screen.getByText('Active: docs')).toBeInTheDocument();
  });

  it('loads the selected base when a base card is clicked', async () => {
    mockStore({
      alpha: { path: './knowledge_docs/alpha', watch: true },
      beta: { path: './knowledge_docs/beta', watch: false },
    });
    setKnowledgeApiMock({
      beta: {
        status: {
          base_id: 'beta',
          folder_path: './knowledge_docs/beta',
          watch: false,
          file_count: 2,
          indexed_count: 2,
        },
        files: {
          base_id: 'beta',
          files: [
            {
              name: 'a.txt',
              path: 'a.txt',
              size: 10,
              modified: '2026-02-09T00:00:00.000Z',
              type: 'txt',
            },
            {
              name: 'b.txt',
              path: 'b.txt',
              size: 20,
              modified: '2026-02-09T00:01:00.000Z',
              type: 'txt',
            },
          ],
          total_size: 30,
          file_count: 2,
        },
      },
    });

    render(<Knowledge />);

    await screen.findByText('Knowledge Bases');
    expect(vi.mocked(global.fetch)).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /beta/i }));

    await waitFor(() => {
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        1,
        API_ENDPOINTS.knowledge.status('beta'),
        undefined
      );
      expect(vi.mocked(global.fetch)).toHaveBeenNthCalledWith(
        2,
        API_ENDPOINTS.knowledge.files('beta'),
        undefined
      );
    });
    expect(screen.getByText('Active: beta')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Active Base' })).not.toBeDisabled();
  });
});
