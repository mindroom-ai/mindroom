import { Config, APIKey } from '@/types/config';

const API_BASE = '/api';

export async function loadConfig(): Promise<Config> {
  const response = await fetch(`${API_BASE}/config/load`, {
    method: 'POST',
  });

  if (!response.ok) {
    throw new Error('Failed to load configuration');
  }

  return response.json();
}

export async function saveConfig(config: Config): Promise<void> {
  const response = await fetch(`${API_BASE}/config/save`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(config),
  });

  if (!response.ok) {
    throw new Error('Failed to save configuration');
  }
}

export async function testModel(modelId: string): Promise<boolean> {
  const response = await fetch(`${API_BASE}/test/model`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ modelId }),
  });

  if (!response.ok) {
    return false;
  }

  const result = await response.json();
  return result.success;
}

export async function encryptAPIKey(provider: string, key: string): Promise<string> {
  const response = await fetch(`${API_BASE}/keys/encrypt`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ provider, key }),
  });

  if (!response.ok) {
    throw new Error('Failed to encrypt API key');
  }

  const result = await response.json();
  return result.encryptedKey;
}

export async function getAvailableTools(): Promise<string[]> {
  const response = await fetch(`${API_BASE}/tools`);

  if (!response.ok) {
    throw new Error('Failed to fetch available tools');
  }

  return response.json();
}

export async function getAvailableRooms(): Promise<string[]> {
  const response = await fetch(`${API_BASE}/rooms`);

  if (!response.ok) {
    throw new Error('Failed to fetch available rooms');
  }

  return response.json();
}
