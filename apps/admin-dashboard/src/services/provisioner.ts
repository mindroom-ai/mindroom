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
    const response = await fetch(`${config.provisionerUrl}/provision`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': config.provisionerApiKey,
      },
      body: JSON.stringify(data),
    })

    if (!response.ok) {
      throw new Error(`Provisioning failed: ${response.statusText}`)
    }

    return response.json()
  },

  async deprovision(appName: string): Promise<void> {
    const response = await fetch(`${config.provisionerUrl}/deprovision/${appName}`, {
      method: 'DELETE',
      headers: {
        'X-API-Key': config.provisionerApiKey,
      },
    })

    if (!response.ok) {
      throw new Error(`Deprovisioning failed: ${response.statusText}`)
    }
  },

  async start(appName: string): Promise<void> {
    const response = await fetch(`${config.provisionerUrl}/instances/${appName}/start`, {
      method: 'POST',
      headers: {
        'X-API-Key': config.provisionerApiKey,
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to start instance: ${response.statusText}`)
    }
  },

  async stop(appName: string): Promise<void> {
    const response = await fetch(`${config.provisionerUrl}/instances/${appName}/stop`, {
      method: 'POST',
      headers: {
        'X-API-Key': config.provisionerApiKey,
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to stop instance: ${response.statusText}`)
    }
  },

  async restart(appName: string): Promise<void> {
    const response = await fetch(`${config.provisionerUrl}/instances/${appName}/restart`, {
      method: 'POST',
      headers: {
        'X-API-Key': config.provisionerApiKey,
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to restart instance: ${response.statusText}`)
    }
  },

  async getLogs(appName: string, lines: number = 100): Promise<string> {
    const response = await fetch(`${config.provisionerUrl}/instances/${appName}/logs?lines=${lines}`, {
      headers: {
        'X-API-Key': config.provisionerApiKey,
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to get logs: ${response.statusText}`)
    }

    return response.text()
  },

  async getHealth(appName: string): Promise<any> {
    const response = await fetch(`${config.provisionerUrl}/instances/${appName}/health`, {
      headers: {
        'X-API-Key': config.provisionerApiKey,
      },
    })

    if (!response.ok) {
      throw new Error(`Failed to get health status: ${response.statusText}`)
    }

    return response.json()
  },
}
