import { describe, expect, it } from 'vitest';

import { getProviderInfo, getProviderList } from './providers';

describe('providers', () => {
  it('lists Vertex Claude providers in dropdown options', () => {
    const providerIds = getProviderList().map(provider => provider.id);

    expect(providerIds).toContain('vertexai_claude');
    expect(providerIds).toContain('anthropic_vertex');
  });

  it('returns metadata for Vertex Claude providers', () => {
    const vertexProvider = getProviderInfo('vertexai_claude');
    const aliasProvider = getProviderInfo('anthropic_vertex');

    expect(vertexProvider.name).toBe('Vertex AI Claude');
    expect(vertexProvider.requiresApiKey).toBe(false);
    expect(aliasProvider.name).toBe('Vertex AI Claude (alias)');
    expect(aliasProvider.requiresApiKey).toBe(false);
  });
});
