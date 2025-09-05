import fetch from 'node-fetch';
import { config } from '../config';
import {
  ProvisionRequest,
  ProvisionResponse,
  DeprovisionRequest,
  DeprovisionResponse,
  UpdateLimitsRequest,
  UpdateLimitsResponse,
  ResourceLimits,
} from '../types';

// Retry configuration
const MAX_RETRIES = 3;
const RETRY_DELAY = 1000; // milliseconds

async function makeRequest<T>(
  url: string,
  method: string,
  body?: any,
  retries = MAX_RETRIES
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.provisioner.timeout);

  try {
    const response = await fetch(url, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': config.provisioner.apiKey || '',
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });

    clearTimeout(timeout);

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Provisioner ${method} error (${response.status}): ${errorText}`);
    }

    return await response.json() as T;
  } catch (error: any) {
    clearTimeout(timeout);

    // Retry on network errors or timeouts
    if (retries > 0 && (error.name === 'AbortError' || error.code === 'ECONNREFUSED')) {
      console.log(`Retrying provisioner request (${MAX_RETRIES - retries + 1}/${MAX_RETRIES})...`);
      await new Promise(resolve => setTimeout(resolve, RETRY_DELAY));
      return makeRequest<T>(url, method, body, retries - 1);
    }

    throw error;
  }
}

export async function provisionInstance(data: ProvisionRequest): Promise<ProvisionResponse> {
  console.log(`📦 Provisioning instance for subscription ${data.subscriptionId}`);

  try {
    const response = await makeRequest<ProvisionResponse>(
      `${config.provisioner.url}/provision`,
      'POST',
      {
        ...data,
        // Generate subdomain if not provided
        subdomain: data.subdomain || generateSubdomain(data.accountId),
      }
    );

    console.log(`✅ Instance provisioned: ${response.appName}`);
    return response;
  } catch (error) {
    console.error('❌ Failed to provision instance:', error);
    throw error;
  }
}

export async function deprovisionInstance(data: DeprovisionRequest): Promise<DeprovisionResponse> {
  console.log(`🗑️ Deprovisioning instance: ${data.appName}`);

  try {
    const response = await makeRequest<DeprovisionResponse>(
      `${config.provisioner.url}/deprovision`,
      'DELETE',
      data
    );

    console.log(`✅ Instance deprovisioned: ${data.appName}`);
    return response;
  } catch (error) {
    console.error('❌ Failed to deprovision instance:', error);
    throw error;
  }
}

export async function updateInstanceLimits(data: UpdateLimitsRequest): Promise<UpdateLimitsResponse> {
  console.log(`📊 Updating resource limits for: ${data.appName}`);

  try {
    const response = await makeRequest<UpdateLimitsResponse>(
      `${config.provisioner.url}/update-limits`,
      'PUT',
      data
    );

    console.log(`✅ Resource limits updated for: ${data.appName}`);
    return response;
  } catch (error) {
    console.error('❌ Failed to update resource limits:', error);
    throw error;
  }
}

// Check provisioner health
export async function checkProvisionerHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${config.provisioner.url}/health`, {
      method: 'GET',
      headers: {
        'X-API-Key': config.provisioner.apiKey || '',
      },
      signal: AbortSignal.timeout(5000), // 5 second timeout
    });

    return response.ok;
  } catch (error) {
    console.error('Provisioner health check failed:', error);
    return false;
  }
}

// Helper function to generate a unique subdomain
function generateSubdomain(accountId: string): string {
  // Use last 8 chars of account ID and a random suffix
  const idPart = accountId.replace(/-/g, '').slice(-8);
  const randomPart = Math.random().toString(36).substring(2, 6);
  return `mr-${idPart}-${randomPart}`.toLowerCase();
}

// Export resource limit helpers
export function getResourceLimits(tier: string): ResourceLimits {
  const tierConfig = config.tiers[tier as keyof typeof config.tiers];

  if (!tierConfig) {
    console.warn(`Unknown tier: ${tier}, using free tier limits`);
    return config.tiers.free;
  }

  return {
    agents: tierConfig.agents,
    messagesPerDay: tierConfig.messagesPerDay,
    memoryMb: tierConfig.memoryMb,
    cpuLimit: tierConfig.cpuLimit,
  };
}

export function getTierFromPriceId(priceId: string): string {
  // Map Stripe price IDs to our tier names
  const priceMap: { [key: string]: string } = {
    [config.stripe.prices.starter]: 'starter',
    [config.stripe.prices.professional]: 'professional',
    [config.stripe.prices.enterprise]: 'enterprise',
  };

  return priceMap[priceId] || 'free';
}
