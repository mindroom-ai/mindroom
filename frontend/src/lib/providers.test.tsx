import { describe, expect, it } from "vitest";

import acceptedProviders from "@/test/fixtures/modelProviders.json";

import { getProviderInfo, getProviderList, PROVIDERS } from "./providers";

describe("providers", () => {
  it("lists Vertex Claude provider in dropdown options", () => {
    const providerIds = getProviderList().map((provider) => provider.id);

    expect(providerIds).toContain("vertexai_claude");
  });

  it("returns metadata for Vertex Claude provider", () => {
    const vertexProvider = getProviderInfo("vertexai_claude");

    expect(vertexProvider.name).toBe("Vertex AI Claude");
    expect(vertexProvider.requiresApiKey).toBe(false);
  });

  it("knows only providers that config load accepts", () => {
    const rejected = Object.keys(PROVIDERS).filter(
      (providerId) => !acceptedProviders.includes(providerId),
    );

    expect(rejected).toEqual([]);
  });
});
