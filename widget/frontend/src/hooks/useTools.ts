import { useState, useEffect } from 'react';
import { API_ENDPOINTS, fetchAPI } from '@/lib/api';

export interface ToolInfo {
  name: string;
  display_name: string;
  description: string;
  category: string;
  status: string;
  setup_type: string;
  icon: string | null;
  requires_config: string[] | null;
  dependencies: string[] | null;
}

export interface ToolsResponse {
  tools: ToolInfo[];
}

export function useTools() {
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchTools() {
      try {
        setLoading(true);
        const response = (await fetchAPI(API_ENDPOINTS.tools)) as ToolsResponse;
        setTools(response.tools);
        setError(null);
      } catch (err) {
        console.error('Failed to fetch tools:', err);
        setError(err instanceof Error ? err.message : 'Failed to fetch tools');
        // Fall back to empty array on error
        setTools([]);
      } finally {
        setLoading(false);
      }
    }

    fetchTools();
  }, []);

  return { tools, loading, error };
}

// Helper function to map backend tool to frontend integration format
export function mapToolToIntegration(tool: ToolInfo) {
  // Map backend status to frontend status
  let status: 'connected' | 'not_connected' | 'available' | 'coming_soon';
  switch (tool.status) {
    case 'available':
      status = 'available';
      break;
    case 'requires_config':
      status = 'not_connected';
      break;
    case 'coming_soon':
      status = 'coming_soon';
      break;
    default:
      status = 'available';
  }

  // Map setup_type
  let setup_type: 'oauth' | 'api_key' | 'special' | 'coming_soon' | 'none';
  switch (tool.setup_type) {
    case 'oauth':
      setup_type = 'oauth';
      break;
    case 'api_key':
      setup_type = 'api_key';
      break;
    case 'special':
      setup_type = 'special';
      break;
    case 'coming_soon':
      setup_type = 'coming_soon';
      break;
    case 'none':
    default:
      setup_type = 'none';
      break;
  }

  return {
    id: tool.name,
    name: tool.display_name,
    description: tool.description,
    category: tool.category,
    icon: tool.icon, // This will need to be mapped to React components
    status,
    setup_type,
    requires_config: tool.requires_config,
    dependencies: tool.dependencies,
  };
}
