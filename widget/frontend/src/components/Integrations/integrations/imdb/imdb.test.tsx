import { describe, it, expect, vi, beforeEach } from 'vitest';
import { imdbIntegration } from './index';

// Mock fetch
global.fetch = vi.fn();

// Mock localStorage
const localStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
};
Object.defineProperty(window, 'localStorage', { value: localStorageMock });

describe('IMDbIntegrationProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
    localStorageMock.getItem.mockReset();
    localStorageMock.setItem.mockReset();
    localStorageMock.removeItem.mockReset();
  });

  describe('getConfig', () => {
    it('should return correct integration configuration', () => {
      const config = imdbIntegration.getConfig();

      expect(config.integration.id).toBe('imdb');
      expect(config.integration.name).toBe('IMDb');
      expect(config.integration.description).toBe('Movie and TV show database');
      expect(config.integration.category).toBe('entertainment');
      expect(config.integration.setup_type).toBe('api_key');
      expect(config.integration.status).toBe('available');
      expect(config.integration.connected).toBe(false);
    });

    it('should provide onAction handler', () => {
      const config = imdbIntegration.getConfig();
      expect(config.onAction).toBeDefined();
      expect(typeof config.onAction).toBe('function');
    });

    it('should provide onDisconnect handler', () => {
      const config = imdbIntegration.getConfig();
      expect(config.onDisconnect).toBeDefined();
      expect(typeof config.onDisconnect).toBe('function');
    });

    it('should provide ConfigComponent', () => {
      const config = imdbIntegration.getConfig();
      expect(config.ConfigComponent).toBeDefined();
    });

    it('should provide checkConnection method', () => {
      const config = imdbIntegration.getConfig();
      expect(config.checkConnection).toBeDefined();
      expect(typeof config.checkConnection).toBe('function');
    });
  });

  describe('loadStatus', () => {
    it('should return connected status when configured', async () => {
      localStorageMock.getItem.mockReturnValue('true');

      const status = await imdbIntegration.loadStatus();

      expect(status.status).toBe('connected');
      expect(status.connected).toBe(true);
    });

    it('should return available status when not configured', async () => {
      localStorageMock.getItem.mockReturnValue(null);
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      const status = await imdbIntegration.loadStatus();

      expect(status.status).toBe('available');
      expect(status.connected).toBe(false);
    });
  });

  describe('onDisconnect', () => {
    it('should remove localStorage and call disconnect endpoint', async () => {
      (global.fetch as any).mockResolvedValueOnce({ ok: true });

      const config = imdbIntegration.getConfig();
      await config.onDisconnect!('imdb');

      expect(localStorageMock.removeItem).toHaveBeenCalledWith('imdb_configured');
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/credentials/imdb'),
        expect.objectContaining({ method: 'DELETE' })
      );
    });

    it('should handle disconnect errors gracefully', async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const config = imdbIntegration.getConfig();

      // Should not throw, just log error
      await expect(config.onDisconnect!('imdb')).resolves.not.toThrow();
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('imdb_configured');
    });
  });

  describe('checkConnection', () => {
    it('should return true when localStorage indicates connected', async () => {
      localStorageMock.getItem.mockReturnValue('true');

      const config = imdbIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(true);
      expect(localStorageMock.getItem).toHaveBeenCalledWith('imdb_configured');
    });

    it('should check backend when localStorage is empty', async () => {
      localStorageMock.getItem.mockReturnValue(null);
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ has_key: true }),
      });

      const config = imdbIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(true);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/credentials/imdb/api-key')
      );
    });

    it('should return false on backend error', async () => {
      localStorageMock.getItem.mockReturnValue(null);
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const config = imdbIntegration.getConfig();
      const isConnected = await config.checkConnection!();

      expect(isConnected).toBe(false);
    });
  });
});
