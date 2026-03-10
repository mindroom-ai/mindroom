import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/lib/api', () => ({
  API_BASE_URL: 'https://backend.example.test',
  withAgentName: (url: string, agentName?: string | null) => {
    const parsed = new URL(url);
    if (agentName) {
      parsed.searchParams.set('agent_name', agentName);
    }
    return parsed.toString();
  },
}));

import { homeAssistantIntegration } from './index';

// Mock fetch
global.fetch = vi.fn();

describe('HomeAssistantIntegrationProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('getConfig', () => {
    it('should return proper configuration', () => {
      const config = homeAssistantIntegration.getConfig();

      expect(config.integration.id).toBe('homeassistant');
      expect(config.integration.name).toBe('Home Assistant');
      expect(config.integration.category).toBe('smart_home');
      expect(config.integration.setup_type).toBe('special');
      expect(config.ConfigComponent).toBeDefined();
    });
  });

  describe('loadStatus', () => {
    it('should return connected status when API returns connected', async () => {
      const mockResponse = {
        connected: true,
        instance_url: 'http://homeassistant.local:8123',
        version: '2024.1.0',
        location_name: 'Home',
        entities_count: 42,
      };

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => mockResponse,
      });

      const status = await homeAssistantIntegration.loadStatus!();

      expect(status.status).toBe('connected');
      expect(status.connected).toBe(true);
      expect(status.details).toEqual({
        instance_url: 'http://homeassistant.local:8123',
        version: '2024.1.0',
        location_name: 'Home',
        entities_count: 42,
      });
      expect(global.fetch).toHaveBeenCalledWith(
        'https://backend.example.test/api/homeassistant/status'
      );
    });

    it('should return available status when not connected', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      const status = await homeAssistantIntegration.loadStatus!();

      expect(status.status).toBe('available');
      expect(status.connected).toBe(false);
    });

    it('should handle API errors gracefully', async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const status = await homeAssistantIntegration.loadStatus!();

      expect(status.status).toBe('available');
      expect(status.connected).toBe(false);
    });

    it('appends agent_name when checking scoped status', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      await homeAssistantIntegration.loadStatus!({ agentName: 'code' });

      expect(global.fetch).toHaveBeenCalledWith(
        'https://backend.example.test/api/homeassistant/status?agent_name=code'
      );
    });
  });

  describe('disconnect', () => {
    it('appends agent_name to the API_BASE_URL-backed disconnect request', async () => {
      (global.fetch as any).mockResolvedValueOnce({ ok: true });

      const config = homeAssistantIntegration.getConfig({ agentName: 'code' });
      await config.onDisconnect!('homeassistant');

      expect(global.fetch).toHaveBeenCalledWith(
        'https://backend.example.test/api/homeassistant/disconnect?agent_name=code',
        expect.objectContaining({ method: 'POST' })
      );
    });
  });
});
