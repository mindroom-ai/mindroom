// Tool configuration type definitions

export type FieldType = 'text' | 'password' | 'number' | 'boolean' | 'select' | 'url';

export interface ToolConfigField {
  name: string;
  label: string;
  type: FieldType;
  required?: boolean;
  default?: any;
  placeholder?: string;
  description?: string;
  options?: Array<{ value: string; label: string }>; // For select type
  validation?: {
    min?: number;
    max?: number;
    pattern?: string;
    message?: string;
  };
}

export interface ToolConfigSchema {
  id: string;
  name: string;
  description: string;
  icon?: string;
  fields: ToolConfigField[];
  category?: 'search' | 'files' | 'communication' | 'development' | 'data' | 'other';
}

// Tool-specific configuration values
export interface ToolConfigValues {
  [key: string]: any;
}

// Complete tool configuration
export interface ToolConfiguration {
  toolId: string;
  enabled: boolean;
  config: ToolConfigValues;
}

// Tool configuration schemas for available tools
export const TOOL_SCHEMAS: Record<string, ToolConfigSchema> = {
  // Search tools
  googlesearch: {
    id: 'googlesearch',
    name: 'Google Search',
    description: 'Search the web using Google',
    category: 'search',
    fields: [
      {
        name: 'api_key',
        label: 'API Key',
        type: 'password',
        required: true,
        placeholder: 'Enter Google API key',
        description: 'Your Google Custom Search API key',
      },
      {
        name: 'search_engine_id',
        label: 'Search Engine ID',
        type: 'text',
        required: true,
        placeholder: 'Enter Search Engine ID',
        description: 'Your Google Custom Search Engine ID',
      },
      {
        name: 'max_results',
        label: 'Max Results',
        type: 'number',
        default: 10,
        validation: {
          min: 1,
          max: 100,
        },
      },
    ],
  },

  tavily: {
    id: 'tavily',
    name: 'Tavily Search',
    description: 'AI-powered search API',
    category: 'search',
    fields: [
      {
        name: 'api_key',
        label: 'API Key',
        type: 'password',
        required: true,
        placeholder: 'Enter Tavily API key',
        description: 'Your Tavily API key',
      },
      {
        name: 'search_depth',
        label: 'Search Depth',
        type: 'select',
        default: 'basic',
        options: [
          { value: 'basic', label: 'Basic' },
          { value: 'advanced', label: 'Advanced' },
        ],
      },
    ],
  },

  duckduckgo: {
    id: 'duckduckgo',
    name: 'DuckDuckGo',
    description: 'Privacy-focused search',
    category: 'search',
    fields: [
      {
        name: 'region',
        label: 'Region',
        type: 'select',
        default: 'wt-wt',
        options: [
          { value: 'wt-wt', label: 'No region' },
          { value: 'us-en', label: 'United States' },
          { value: 'uk-en', label: 'United Kingdom' },
          { value: 'de-de', label: 'Germany' },
        ],
      },
      {
        name: 'safe_search',
        label: 'Safe Search',
        type: 'boolean',
        default: true,
      },
    ],
  },

  // Communication tools
  email: {
    id: 'email',
    name: 'Email',
    description: 'Send and receive emails',
    category: 'communication',
    fields: [
      {
        name: 'smtp_host',
        label: 'SMTP Host',
        type: 'text',
        required: true,
        placeholder: 'smtp.gmail.com',
      },
      {
        name: 'smtp_port',
        label: 'SMTP Port',
        type: 'number',
        default: 587,
        validation: {
          min: 1,
          max: 65535,
        },
      },
      {
        name: 'username',
        label: 'Username',
        type: 'text',
        required: true,
        placeholder: 'your-email@example.com',
      },
      {
        name: 'password',
        label: 'Password',
        type: 'password',
        required: true,
        placeholder: 'Enter password or app-specific password',
      },
      {
        name: 'use_tls',
        label: 'Use TLS',
        type: 'boolean',
        default: true,
      },
    ],
  },

  telegram: {
    id: 'telegram',
    name: 'Telegram',
    description: 'Send messages via Telegram',
    category: 'communication',
    fields: [
      {
        name: 'bot_token',
        label: 'Bot Token',
        type: 'password',
        required: true,
        placeholder: 'Enter Telegram bot token',
        description: 'Your Telegram bot API token',
      },
      {
        name: 'default_chat_id',
        label: 'Default Chat ID',
        type: 'text',
        placeholder: 'Enter default chat ID (optional)',
        description: 'Default chat/channel ID for messages',
      },
    ],
  },

  // Development tools
  github: {
    id: 'github',
    name: 'GitHub',
    description: 'Interact with GitHub repositories',
    category: 'development',
    fields: [
      {
        name: 'access_token',
        label: 'Access Token',
        type: 'password',
        required: true,
        placeholder: 'Enter GitHub personal access token',
        description: 'GitHub personal access token with required permissions',
      },
      {
        name: 'default_owner',
        label: 'Default Owner',
        type: 'text',
        placeholder: 'github-username',
        description: 'Default repository owner/organization',
      },
    ],
  },

  docker: {
    id: 'docker',
    name: 'Docker',
    description: 'Manage Docker containers',
    category: 'development',
    fields: [
      {
        name: 'socket_path',
        label: 'Docker Socket',
        type: 'text',
        default: '/var/run/docker.sock',
        placeholder: '/var/run/docker.sock',
      },
      {
        name: 'timeout',
        label: 'Timeout (seconds)',
        type: 'number',
        default: 30,
        validation: {
          min: 1,
          max: 300,
        },
      },
    ],
  },

  // File and data tools
  shell: {
    id: 'shell',
    name: 'Shell',
    description: 'Execute shell commands',
    category: 'development',
    fields: [
      {
        name: 'allowed_commands',
        label: 'Allowed Commands',
        type: 'text',
        placeholder: 'ls,cat,echo (comma-separated)',
        description: 'Comma-separated list of allowed commands (leave empty for all)',
      },
      {
        name: 'working_directory',
        label: 'Working Directory',
        type: 'text',
        placeholder: '/home/user',
        description: 'Default working directory',
      },
      {
        name: 'timeout',
        label: 'Command Timeout (seconds)',
        type: 'number',
        default: 30,
        validation: {
          min: 1,
          max: 300,
        },
      },
    ],
  },

  python: {
    id: 'python',
    name: 'Python',
    description: 'Execute Python code',
    category: 'development',
    fields: [
      {
        name: 'interpreter',
        label: 'Python Interpreter',
        type: 'text',
        default: 'python3',
        placeholder: 'python3',
      },
      {
        name: 'virtual_env',
        label: 'Virtual Environment',
        type: 'text',
        placeholder: '/path/to/venv',
        description: 'Path to virtual environment (optional)',
      },
      {
        name: 'timeout',
        label: 'Execution Timeout (seconds)',
        type: 'number',
        default: 30,
        validation: {
          min: 1,
          max: 300,
        },
      },
    ],
  },

  file: {
    id: 'file',
    name: 'File System',
    description: 'Read and write files',
    category: 'files',
    fields: [
      {
        name: 'base_path',
        label: 'Base Path',
        type: 'text',
        required: true,
        placeholder: '/home/user/workspace',
        description: 'Base directory for file operations',
      },
      {
        name: 'allow_write',
        label: 'Allow Write Operations',
        type: 'boolean',
        default: true,
      },
      {
        name: 'max_file_size',
        label: 'Max File Size (MB)',
        type: 'number',
        default: 10,
        validation: {
          min: 1,
          max: 100,
        },
      },
    ],
  },

  // Data analysis tools
  yfinance: {
    id: 'yfinance',
    name: 'Yahoo Finance',
    description: 'Fetch financial data',
    category: 'data',
    fields: [
      {
        name: 'proxy',
        label: 'Proxy URL',
        type: 'url',
        placeholder: 'http://proxy.example.com:8080',
        description: 'Proxy server URL (optional)',
      },
      {
        name: 'timeout',
        label: 'Request Timeout (seconds)',
        type: 'number',
        default: 10,
        validation: {
          min: 1,
          max: 60,
        },
      },
    ],
  },

  // Tools that don't need configuration
  calculator: {
    id: 'calculator',
    name: 'Calculator',
    description: 'Perform mathematical calculations',
    category: 'other',
    fields: [],
  },

  wikipedia: {
    id: 'wikipedia',
    name: 'Wikipedia',
    description: 'Search and fetch Wikipedia articles',
    category: 'search',
    fields: [
      {
        name: 'language',
        label: 'Language',
        type: 'select',
        default: 'en',
        options: [
          { value: 'en', label: 'English' },
          { value: 'es', label: 'Spanish' },
          { value: 'fr', label: 'French' },
          { value: 'de', label: 'German' },
          { value: 'ja', label: 'Japanese' },
        ],
      },
    ],
  },

  arxiv: {
    id: 'arxiv',
    name: 'arXiv',
    description: 'Search academic papers on arXiv',
    category: 'search',
    fields: [
      {
        name: 'max_results',
        label: 'Max Results',
        type: 'number',
        default: 10,
        validation: {
          min: 1,
          max: 50,
        },
      },
    ],
  },

  csv: {
    id: 'csv',
    name: 'CSV Handler',
    description: 'Read and write CSV files',
    category: 'data',
    fields: [
      {
        name: 'delimiter',
        label: 'Delimiter',
        type: 'select',
        default: ',',
        options: [
          { value: ',', label: 'Comma (,)' },
          { value: ';', label: 'Semicolon (;)' },
          { value: '\t', label: 'Tab' },
          { value: '|', label: 'Pipe (|)' },
        ],
      },
      {
        name: 'encoding',
        label: 'Encoding',
        type: 'select',
        default: 'utf-8',
        options: [
          { value: 'utf-8', label: 'UTF-8' },
          { value: 'latin-1', label: 'Latin-1' },
          { value: 'ascii', label: 'ASCII' },
        ],
      },
    ],
  },

  pandas: {
    id: 'pandas',
    name: 'Pandas',
    description: 'Data analysis with Pandas',
    category: 'data',
    fields: [
      {
        name: 'max_rows',
        label: 'Max Display Rows',
        type: 'number',
        default: 100,
        validation: {
          min: 10,
          max: 1000,
        },
      },
      {
        name: 'max_columns',
        label: 'Max Display Columns',
        type: 'number',
        default: 20,
        validation: {
          min: 5,
          max: 100,
        },
      },
    ],
  },

  newspaper: {
    id: 'newspaper',
    name: 'Newspaper',
    description: 'Extract articles from news websites',
    category: 'search',
    fields: [
      {
        name: 'user_agent',
        label: 'User Agent',
        type: 'text',
        placeholder: 'Mozilla/5.0...',
        description: 'Custom user agent string (optional)',
      },
      {
        name: 'request_timeout',
        label: 'Request Timeout (seconds)',
        type: 'number',
        default: 10,
        validation: {
          min: 1,
          max: 60,
        },
      },
    ],
  },

  website: {
    id: 'website',
    name: 'Website Scraper',
    description: 'Scrape content from websites',
    category: 'search',
    fields: [
      {
        name: 'user_agent',
        label: 'User Agent',
        type: 'text',
        placeholder: 'Mozilla/5.0...',
        description: 'Custom user agent string (optional)',
      },
      {
        name: 'timeout',
        label: 'Request Timeout (seconds)',
        type: 'number',
        default: 10,
        validation: {
          min: 1,
          max: 60,
        },
      },
      {
        name: 'follow_redirects',
        label: 'Follow Redirects',
        type: 'boolean',
        default: true,
      },
    ],
  },

  jina: {
    id: 'jina',
    name: 'Jina',
    description: 'Neural search with Jina',
    category: 'search',
    fields: [
      {
        name: 'api_key',
        label: 'API Key',
        type: 'password',
        required: true,
        placeholder: 'Enter Jina API key',
        description: 'Your Jina API key',
      },
      {
        name: 'endpoint',
        label: 'Endpoint URL',
        type: 'url',
        placeholder: 'https://api.jina.ai',
        description: 'Jina API endpoint',
      },
    ],
  },
};

// Helper to get unconfigured tools (tools without schemas)
export function getUnconfiguredTools(tools: string[]): string[] {
  return tools.filter(tool => !TOOL_SCHEMAS[tool]);
}

// Helper to check if a tool needs configuration
export function toolNeedsConfiguration(toolId: string): boolean {
  const schema = TOOL_SCHEMAS[toolId];
  if (!schema) return false;
  return schema.fields.some(field => field.required);
}

// Helper to get default configuration for a tool
export function getDefaultToolConfig(toolId: string): ToolConfigValues {
  const schema = TOOL_SCHEMAS[toolId];
  if (!schema) return {};

  const config: ToolConfigValues = {};
  schema.fields.forEach(field => {
    if (field.default !== undefined) {
      config[field.name] = field.default;
    }
  });
  return config;
}

// Helper to check if a tool needs configuration
export function needsConfiguration(toolId: string): boolean {
  const schema = TOOL_SCHEMAS[toolId];
  if (!schema) return false;
  return schema.fields.some(field => field.required);
}

// Helper to check if a tool is configured
export function isToolConfigured(toolId: string, config?: ToolConfigValues): boolean {
  const schema = TOOL_SCHEMAS[toolId];
  if (!schema) return true; // No schema means no config needed

  if (!needsConfiguration(toolId)) return true; // No required fields
  if (!config) return false; // Has required fields but no config

  return schema.fields
    .filter(field => field.required)
    .every(field => config[field.name] && config[field.name] !== '');
}

// Helper to validate tool configuration
export function validateToolConfig(
  toolId: string,
  config: ToolConfigValues
): Record<string, string> {
  const schema = TOOL_SCHEMAS[toolId];
  if (!schema) return {};

  const errors: Record<string, string> = {};

  schema.fields.forEach(field => {
    const value = config[field.name];

    // Check required fields
    if (field.required && (!value || value === '')) {
      errors[field.name] = `${field.label} is required`;
      return;
    }

    // Type-specific validation
    if (value !== undefined && value !== '' && field.validation) {
      if (field.type === 'number') {
        const numValue = Number(value);
        if (field.validation.min !== undefined && numValue < field.validation.min) {
          errors[field.name] = `Must be at least ${field.validation.min}`;
        }
        if (field.validation.max !== undefined && numValue > field.validation.max) {
          errors[field.name] = `Must be at most ${field.validation.max}`;
        }
      }
      if (field.validation.pattern) {
        const regex = new RegExp(field.validation.pattern);
        if (!regex.test(String(value))) {
          errors[field.name] = field.validation.message || 'Invalid format';
        }
      }
    }
  });

  return errors;
}
