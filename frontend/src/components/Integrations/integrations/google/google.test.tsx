import { describe, it, expect, vi, beforeEach } from 'vitest';
import { googleIntegration } from './index';

// Mock fetch
global.fetch = vi.fn();

describe('GoogleIntegrationProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  describe('getConfig', () => {
    it('should return correct integration configuration', () => {
      const config = googleIntegration.getConfig();

      expect(config.integration.id).toBe('google');
      expect(config.integration.name).toBe('Google Services');
      expect(config.integration.description).toBe('Gmail, Calendar, and Drive integration');
      expect(config.integration.category).toBe('email');
      expect(config.integration.setup_type).toBe('special');
      expect(config.integration.status).toBe('available');
      expect(config.integration.connected).toBe(false);
    });

    it('should provide ConfigComponent', () => {
      const config = googleIntegration.getConfig();
      expect(config.ConfigComponent).toBeDefined();
    });
  });

  describe('loadStatus', () => {
    it('should return connected status when configured', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: true }),
      });

      const status = await googleIntegration.loadStatus();

      expect(status.status).toBe('connected');
      expect(status.connected).toBe(true);
      expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining('/api/google/status'));
    });

    it('should return available status when not configured', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      const status = await googleIntegration.loadStatus();

      expect(status.status).toBe('available');
      expect(status.connected).toBe(false);
    });

    it('should handle fetch errors gracefully', async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const status = await googleIntegration.loadStatus();

      expect(status.status).toBe('available');
      expect(status.connected).toBe(false);
    });

    it('appends agent_name when checking scoped status', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      await googleIntegration.loadStatus({ agentName: 'code' });

      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/google/status?agent_name=code')
      );
    });
  });

  describe('disconnect', () => {
    it('appends agent_name for scoped disconnect', async () => {
      (global.fetch as any).mockResolvedValueOnce({ ok: true });

      const config = googleIntegration.getConfig({ agentName: 'code' });
      await config.onDisconnect!('google');

      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/google/disconnect?agent_name=code'),
        expect.objectContaining({ method: 'POST' })
      );
    });
  });
});
