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
        'Authorization': `Bearer ${config.provisioner.apiKey || ''}`,
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
    // Call the K8s provisioner with the correct field names
    const k8sResponse = await makeRequest<any>(
      `${config.provisioner.url}/provision`,
      'POST',
      {
        subscription_id: data.subscriptionId,
        account_id: data.accountId,
        tier: data.tier,
        // Don't pass subdomain - let the provisioner generate it
      }
    );

    // Map K8s provisioner response to our expected format
    const response: ProvisionResponse = {
      appName: k8sResponse.customer_id, // Use customer_id as app name
      subdomain: k8sResponse.customer_id, // Use customer_id as subdomain too
      frontendUrl: k8sResponse.frontend_url,
      backendUrl: k8sResponse.api_url, // Map api_url to backendUrl
      status: k8sResponse.success ? 'success' : 'pending',
      message: k8sResponse.message,
      authToken: k8sResponse.auth_token, // Store the auth token
    };

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
    // Call K8s provisioner with correct field names
    const k8sResponse = await makeRequest<any>(
      `${config.provisioner.url}/deprovision`,
      'DELETE',
      {
        customer_id: data.appName, // Use appName as customer_id
        subscription_id: data.subscriptionId || 'unknown', // Pass subscription ID if available
      }
    );

    const response: DeprovisionResponse = {
      status: k8sResponse.success ? 'success' : 'pending',
      message: k8sResponse.message,
    };

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
        'Authorization': `Bearer ${config.provisioner.apiKey || ''}`,
      },
      signal: AbortSignal.timeout(5000), // 5 second timeout
    });

    return response.ok;
  } catch (error) {
    console.error('Provisioner health check failed:', error);
    return false;
  }
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
