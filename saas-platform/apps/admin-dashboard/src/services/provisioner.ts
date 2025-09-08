import { config } from '../config'

interface ProvisionRequest {
  account_id: string
  instance_name: string
  subdomain: string
  cpu_limit?: string
  memory_limit?: string
  storage_limit_gb?: number
}

interface ProvisionResponse {
  app_name: string
  status: string
  message?: string
}

export const provisionerService = {
  async provision(data: ProvisionRequest): Promise<ProvisionResponse> {
    const response = await fetch(`${config.apiUrl}/instances`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(data),
    })

    if (!response.ok) {
      throw new Error(`Provisioning failed: ${response.statusText}`)
    }

    return response.json()
  },

  async deprovision(appName: string): Promise<void> {
    const response = await fetch(`${config.apiUrl}/instances/${appName}`, {
      method: 'DELETE',
    })

    if (!response.ok) {
      throw new Error(`Deprovisioning failed: ${response.statusText}`)
    }
  },

  async start(instanceId: string): Promise<void> {
    const response = await fetch(`${config.apiUrl}/instances/${instanceId}/start`, {
      method: 'POST',
    })

    if (!response.ok) {
      throw new Error(`Failed to start instance: ${response.statusText}`)
    }
  },

  async stop(instanceId: string): Promise<void> {
    const response = await fetch(`${config.apiUrl}/instances/${instanceId}/stop`, {
      method: 'POST',
    })

    if (!response.ok) {
      throw new Error(`Failed to stop instance: ${response.statusText}`)
    }
  },

  async restart(instanceId: string): Promise<void> {
    const response = await fetch(`${config.apiUrl}/instances/${instanceId}/restart`, {
      method: 'POST',
    })

    if (!response.ok) {
      throw new Error(`Failed to restart instance: ${response.statusText}`)
    }
  },

  async getLogs(instanceId: string, lines: number = 100): Promise<string> {
    const response = await fetch(`${config.apiUrl}/instances/${instanceId}/logs?lines=${lines}`)

    if (!response.ok) {
      throw new Error(`Failed to get logs: ${response.statusText}`)
    }

    return response.text()
  },

  async getHealth(instanceId: string): Promise<any> {
    const response = await fetch(`${config.apiUrl}/instances/${instanceId}/health`)

    if (!response.ok) {
      throw new Error(`Failed to get health status: ${response.statusText}`)
    }

    return response.json()
  },
}
