import { useMemo } from 'react';
import { API_ENDPOINTS, fetchAPI } from '@/lib/api';
import { useFetchData } from './useFetchData';

export interface ToolInfo {
  name: string;
  display_name: string;
  description: string;
  category: string;
  status: string;
  setup_type: string;
  icon: string | null;
  icon_color: string | null;
  config_fields: any[] | null;
  dependencies: string[] | null;
  auth_provider?: string | null;
  docs_url?: string | null;
  helper_text?: string | null;
}

export interface ToolsResponse {
  tools: ToolInfo[];
}

const DEFAULT: ToolInfo[] = [];

export function useTools() {
  const fetcher = useMemo(
    () => async () => {
      const response = (await fetchAPI(API_ENDPOINTS.tools)) as ToolsResponse;
      return response.tools;
    },
    []
  );
  const { data: tools, ...rest } = useFetchData(fetcher, DEFAULT);
  return { tools, ...rest };
}

// Helper function to map backend tool to frontend integration format
export function mapToolToIntegration(tool: ToolInfo) {
  // Map backend status to frontend status
  let status: 'connected' | 'not_connected' | 'available' | 'coming_soon';
  switch (tool.status) {
    case 'available':
      // For tools that require configuration, 'available' means they are configured
      if (
        tool.setup_type === 'api_key' ||
        tool.setup_type === 'oauth' ||
        tool.setup_type === 'special'
      ) {
        status = 'connected';
      } else {
        status = 'available';
      }
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
    icon_color: tool.icon_color,
    status,
    setup_type,
    config_fields: tool.config_fields,
    dependencies: tool.dependencies,
    docs_url: tool.docs_url,
    helper_text: tool.helper_text,
  };
}
