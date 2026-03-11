import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import { HomeAssistantIntegration } from './HomeAssistantIntegration';

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

describe('HomeAssistantIntegration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ connected: false, has_credentials: false, entities_count: 0 }),
    }) as any;
  });

  it('shows the live API callback URL in OAuth setup instructions', async () => {
    render(<HomeAssistantIntegration />);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith('/api/homeassistant/status');
    });

    expect(
      screen.getByText(`${window.location.origin}/api/homeassistant/callback`)
    ).toBeInTheDocument();
  });
});
