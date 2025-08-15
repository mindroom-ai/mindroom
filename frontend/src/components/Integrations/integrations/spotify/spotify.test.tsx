import { describe, it, expect, vi, beforeEach } from 'vitest';
import { spotifyIntegration } from './index';

// Mock fetch and window.open
global.fetch = vi.fn();
global.window.open = vi.fn();

// Mock localStorage
const localStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
};
Object.defineProperty(window, 'localStorage', { value: localStorageMock });

describe('SpotifyIntegrationProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
    localStorageMock.getItem.mockReset();
    localStorageMock.setItem.mockReset();
    localStorageMock.removeItem.mockReset();
  });

  describe('getConfig', () => {
    it('should return correct integration configuration', () => {
      const config = spotifyIntegration.getConfig();

      expect(config.integration.id).toBe('spotify');
      expect(config.integration.name).toBe('Spotify');
      expect(config.integration.description).toBe('Music streaming service integration');
      expect(config.integration.category).toBe('entertainment');
      expect(config.integration.setup_type).toBe('oauth');
      expect(config.integration.status).toBe('available');
      expect(config.integration.connected).toBe(false);
    });

    it('should provide onAction handler', () => {
      const config = spotifyIntegration.getConfig();
      expect(config.onAction).toBeDefined();
      expect(typeof config.onAction).toBe('function');
    });

    it('should provide onDisconnect handler', () => {
      const config = spotifyIntegration.getConfig();
      expect(config.onDisconnect).toBeDefined();
      expect(typeof config.onDisconnect).toBe('function');
    });

    it('should provide checkConnection method', () => {
      const config = spotifyIntegration.getConfig();
      expect(config.checkConnection).toBeDefined();
      expect(typeof config.checkConnection).toBe('function');
    });
  });

  describe('loadStatus', () => {
    it('should return connected status when configured', async () => {
      localStorageMock.getItem.mockReturnValue('true');

      const status = await spotifyIntegration.loadStatus();

      expect(status.status).toBe('connected');
      expect(status.connected).toBe(true);
    });

    it('should return available status when not configured', async () => {
      localStorageMock.getItem.mockReturnValue(null);
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      const status = await spotifyIntegration.loadStatus();

      expect(status.status).toBe('available');
      expect(status.connected).toBe(false);
    });
  });

  describe('onAction (connect)', () => {
    it('should initiate OAuth flow', async () => {
      const mockAuthWindow = { closed: false };
      (global.window.open as any).mockReturnValue(mockAuthWindow);
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ auth_url: 'https://spotify.com/auth' }),
      });

      const config = spotifyIntegration.getConfig();
      const connectPromise = config.onAction(config.integration);

      // Simulate window closing after a short delay
      setTimeout(() => {
        mockAuthWindow.closed = true;
      }, 100);

      await connectPromise;

      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/integrations/spotify/connect'),
        expect.objectContaining({ method: 'POST' })
      );
      expect(global.window.open).toHaveBeenCalledWith(
        'https://spotify.com/auth',
        '_blank',
        'width=500,height=600'
      );
    });

    it('should handle connection errors', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: false,
        json: async () => ({ detail: 'Connection failed' }),
      });

      const config = spotifyIntegration.getConfig();

      await expect(config.onAction(config.integration)).rejects.toThrow('Connection failed');
    });
  });

  describe('onDisconnect', () => {
    it('should remove localStorage and call disconnect endpoint', async () => {
      (global.fetch as any).mockResolvedValueOnce({ ok: true });

      const config = spotifyIntegration.getConfig();
      await config.onDisconnect!('spotify');

      expect(localStorageMock.removeItem).toHaveBeenCalledWith('spotify_configured');
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/integrations/spotify/disconnect'),
        expect.objectContaining({ method: 'POST' })
      );
    });

    it('should handle disconnect errors gracefully', async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const config = spotifyIntegration.getConfig();

      // Should not throw, just log error
      await expect(config.onDisconnect!('spotify')).resolves.not.toThrow();
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('spotify_configured');
    });
  });

  describe('checkConnection', () => {
    it('should return true when localStorage indicates connected', async () => {
      localStorageMock.getItem.mockReturnValue('true');

      const config = spotifyIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(true);
      expect(localStorageMock.getItem).toHaveBeenCalledWith('spotify_configured');
    });

    it('should check backend when localStorage is empty', async () => {
      localStorageMock.getItem.mockReturnValue(null);
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: true }),
      });

      const config = spotifyIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(true);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/integrations/spotify/status')
      );
    });

    it('should return false on backend error', async () => {
      localStorageMock.getItem.mockReturnValue(null);
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const config = spotifyIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(false);
    });
  });
});
