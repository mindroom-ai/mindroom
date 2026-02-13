import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Credentials } from './Credentials';

const mockToast = vi.fn();

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

global.fetch = vi.fn();

describe('Credentials', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  it('loads services and selected service credentials', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['username', 'token'],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: { username: 'x-access-token', token: 'ghp_test' },
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect(screen.getByText('Configured')).toBeInTheDocument();
      expect(screen.getByText('Keys: username, token')).toBeInTheDocument();
    });

    const editor = screen.getByPlaceholderText('{"api_key":"..."}') as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"username": "x-access-token"');
      expect(editor.value).toContain('"token": "ghp_test"');
    });
  });

  it('saves credentials JSON for selected service', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: {},
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: 'success',
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['username', 'token'],
        }),
      });

    render(<Credentials />);

    await screen.findByText('Empty');
    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    const editor = screen.getByPlaceholderText('{"api_key":"..."}');
    fireEvent.change(editor, {
      target: { value: '{"username":"x-access-token","token":"ghp_updated"}' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8765/api/credentials/github_private',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            credentials: { username: 'x-access-token', token: 'ghp_updated' },
          }),
        }
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Credentials saved',
        description: "Updated credentials for 'github_private'.",
      });
    });
  });

  it('shows error and does not save when JSON is invalid', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: {},
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.change(screen.getByPlaceholderText('{"api_key":"..."}'), {
      target: { value: '{"broken_json": ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(screen.getByText('Credentials must be valid JSON')).toBeInTheDocument();
    });
    expect((global.fetch as any).mock.calls).toHaveLength(3);
  });

  it('deletes selected service credentials', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['token'],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: { token: 'ghp_test' },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: 'success',
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith("Delete credentials for 'github_private'?");
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8765/api/credentials/github_private',
        {
          method: 'DELETE',
        }
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Credentials deleted',
        description: "Removed credentials for 'github_private'.",
      });
    });
  });
});
