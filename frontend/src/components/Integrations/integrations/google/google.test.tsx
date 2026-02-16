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

    it('should provide onAction handler', () => {
      const config = googleIntegration.getConfig();
      expect(config.onAction).toBeDefined();
      expect(typeof config.onAction).toBe('function');
    });

    it('should provide ConfigComponent', () => {
      const config = googleIntegration.getConfig();
      expect(config.ConfigComponent).toBeDefined();
    });

    it('should provide checkConnection method', () => {
      const config = googleIntegration.getConfig();
      expect(config.checkConnection).toBeDefined();
      expect(typeof config.checkConnection).toBe('function');
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
  });

  describe('checkConnection', () => {
    it('should return true when configured', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: true }),
      });

      const config = googleIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(true);
    });

    it('should return false when not configured', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      const config = googleIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(false);
    });

    it('should return false on error', async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const config = googleIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(false);
    });
  });
});
