import { describe, expect, it } from 'vitest';

import { getProviderInfo, getProviderList } from './providers';

describe('providers', () => {
  it('lists Vertex Claude provider in dropdown options', () => {
    const providerIds = getProviderList().map(provider => provider.id);

    expect(providerIds).toContain('vertexai_claude');
  });

  it('returns metadata for Vertex Claude provider', () => {
    const vertexProvider = getProviderInfo('vertexai_claude');

    expect(vertexProvider.name).toBe('Vertex AI Claude');
    expect(vertexProvider.requiresApiKey).toBe(false);
  });
});
