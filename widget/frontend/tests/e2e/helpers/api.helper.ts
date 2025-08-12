import { APIRequestContext } from '@playwright/test';

const API_BASE = 'http://localhost:8765';

export class ApiHelper {
  constructor(private request: APIRequestContext) {}

  async clearAllCredentials() {
    // Get list of services with credentials
    const response = await this.request.get(`${API_BASE}/api/credentials/list`);
    const services = await response.json();

    // Delete credentials for each service
    for (const service of services) {
      await this.request.delete(`${API_BASE}/api/credentials/${service}`);
    }
  }

  async setCredentials(service: string, credentials: Record<string, string>) {
    const response = await this.request.post(`${API_BASE}/api/credentials/${service}`, {
      data: { credentials },
    });
    return response.ok();
  }

  async getCredentialStatus(service: string) {
    const response = await this.request.get(`${API_BASE}/api/credentials/${service}/status`);
    return response.json();
  }

  async getToolStatus(toolName: string) {
    const response = await this.request.get(`${API_BASE}/api/tools`);
    const data = await response.json();
    const tool = data.tools.find((t: any) => t.name === toolName);
    return tool?.status;
  }
}
