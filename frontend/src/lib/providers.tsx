import { ReactElement } from 'react';
import {
  Ollama,
  OpenRouter,
  Groq,
  DeepSeek,
  Together,
  Mistral,
  Cohere,
  XAI,
  Cerebras,
} from '@lobehub/icons';
import { Brain } from 'lucide-react';

export interface ProviderInfo {
  id: string;
  name: string;
  description?: string;
  color: string;
  icon: (className?: string) => ReactElement;
  requiresApiKey: boolean;
}

export const PROVIDERS: Record<string, ProviderInfo> = {
  openai: {
    id: 'openai',
    name: 'OpenAI',
    description: 'Configure your OpenAI API key for GPT models',
    color: 'bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20',
    icon: (className = 'h-5 w-5') => (
      <svg className={className} viewBox="0 0 24 24" fill="currentColor">
        <path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.975 5.975 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855l-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.791a4.494 4.494 0 0 1-.676 8.105v-5.678a.79.79 0 0 0-.407-.667zm2.01-3.023l-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.5 4.5 0 0 1 6.68 4.66zm-12.64 4.135l-2.02-1.164a.08.08 0 0 1-.038-.057V6.075a4.5 4.5 0 0 1 7.375-3.453l-.142.08L8.704 5.46a.795.795 0 0 0-.393.681zm1.097-2.365l2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z" />
      </svg>
    ),
    requiresApiKey: true,
  },
  anthropic: {
    id: 'anthropic',
    name: 'Anthropic',
    description: 'Configure your Anthropic API key for Claude models',
    color: 'bg-purple-500/10 text-purple-600 dark:text-purple-400 border-purple-500/20',
    icon: (className = 'h-5 w-5') => (
      <svg className={className} viewBox="0 0 24 24" fill="currentColor">
        <path d="M17.55 3L24 21h-3.77l-1.48-4.17h-6.5L10.77 21H7l6.45-18h4.1zm-3.3 5.58L11.66 16h5.18l-2.59-7.42zM0 21l6.45-18h4.1L17 21h-3.77l-1.48-4.17h-6.5L3.77 21H0zm8.25-7.58h5.18l-2.59-7.42-2.59 7.42z" />
      </svg>
    ),
    requiresApiKey: true,
  },
  ollama: {
    id: 'ollama',
    name: 'Ollama',
    description: 'Local Ollama server',
    color: 'bg-orange-500/10 text-orange-600 dark:text-orange-400 border-orange-500/20',
    icon: (className = 'h-5 w-5') => <Ollama className={className} />,
    requiresApiKey: false,
  },
  openrouter: {
    id: 'openrouter',
    name: 'OpenRouter',
    description: 'Configure your OpenRouter API key',
    color: 'bg-blue-500/10 text-blue-600 dark:text-blue-400 border-blue-500/20',
    icon: (className = 'h-5 w-5') => <OpenRouter className={className} />,
    requiresApiKey: true,
  },
  gemini: {
    id: 'gemini',
    name: 'Google Gemini',
    description: 'Configure your Google API key for Gemini models',
    color: 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 border-cyan-500/20',
    icon: (className = 'h-5 w-5') => (
      <svg className={className} viewBox="0 0 24 24" fill="currentColor">
        <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
        <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
        <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
        <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
      </svg>
    ),
    requiresApiKey: true,
  },
  google: {
    id: 'google',
    name: 'Google Gemini',
    description: 'Configure your Google API key for Gemini models',
    color: 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 border-cyan-500/20',
    icon: (className = 'h-5 w-5') => (
      <svg className={className} viewBox="0 0 24 24" fill="currentColor">
        <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
        <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
        <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
        <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
      </svg>
    ),
    requiresApiKey: true,
  },
  groq: {
    id: 'groq',
    name: 'Groq',
    description: 'Configure your Groq API key for fast inference',
    color: 'bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 border-yellow-500/20',
    icon: (className = 'h-5 w-5') => <Groq className={className} />,
    requiresApiKey: true,
  },
  deepseek: {
    id: 'deepseek',
    name: 'DeepSeek',
    description: 'Configure your DeepSeek API key',
    color: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border-indigo-500/20',
    icon: (className = 'h-5 w-5') => <DeepSeek className={className} />,
    requiresApiKey: true,
  },
  together: {
    id: 'together',
    name: 'Together AI',
    description: 'Configure your Together AI API key',
    color: 'bg-pink-500/10 text-pink-600 dark:text-pink-400 border-pink-500/20',
    icon: (className = 'h-5 w-5') => <Together className={className} />,
    requiresApiKey: true,
  },
  mistral: {
    id: 'mistral',
    name: 'Mistral',
    description: 'Configure your Mistral API key',
    color: 'bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20',
    icon: (className = 'h-5 w-5') => <Mistral className={className} />,
    requiresApiKey: true,
  },
  perplexity: {
    id: 'perplexity',
    name: 'Perplexity',
    description: 'Configure your Perplexity API key',
    color: 'bg-teal-500/10 text-teal-600 dark:text-teal-400 border-teal-500/20',
    icon: (className = 'h-5 w-5') => (
      <svg className={className} viewBox="0 0 24 24" fill="currentColor">
        <path d="M19.5 3.75h-7.5v5.25h7.5c1.45 0 2.625-1.175 2.625-2.625S20.95 3.75 19.5 3.75zm0 3.75h-6v-1.5h6c.41 0 .75.34.75.75s-.34.75-.75.75zM12 3.75H4.5C3.05 3.75 1.875 4.925 1.875 6.375S3.05 9 4.5 9H12V3.75zM4.5 7.5c-.41 0-.75-.34-.75-.75s.34-.75.75-.75h6v1.5h-6zm15 7.5H12v5.25h7.5c1.45 0 2.625-1.175 2.625-2.625S20.95 15 19.5 15zm0 3.75h-6v-1.5h6c.41 0 .75.34.75.75s-.34.75-.75.75zM12 15H4.5c-1.45 0-2.625 1.175-2.625 2.625S3.05 20.25 4.5 20.25H12V15zm-7.5 3.75c-.41 0-.75-.34-.75-.75s.34-.75.75-.75h6v1.5h-6z" />
      </svg>
    ),
    requiresApiKey: true,
  },
  cohere: {
    id: 'cohere',
    name: 'Cohere',
    description: 'Configure your Cohere API key',
    color: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/20',
    icon: (className = 'h-5 w-5') => <Cohere className={className} />,
    requiresApiKey: true,
  },
  xai: {
    id: 'xai',
    name: 'xAI',
    description: 'Configure your xAI API key for Grok models',
    color: 'bg-violet-500/10 text-violet-600 dark:text-violet-400 border-violet-500/20',
    icon: (className = 'h-5 w-5') => <XAI className={className} />,
    requiresApiKey: true,
  },
  grok: {
    id: 'grok',
    name: 'Grok',
    description: 'Configure your xAI API key for Grok models',
    color: 'bg-violet-500/10 text-violet-600 dark:text-violet-400 border-violet-500/20',
    icon: (className = 'h-5 w-5') => <XAI className={className} />,
    requiresApiKey: true,
  },
  cerebras: {
    id: 'cerebras',
    name: 'Cerebras',
    description: 'Configure your Cerebras API key for fast inference',
    color: 'bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20',
    icon: (className = 'h-5 w-5') => <Cerebras className={className} />,
    requiresApiKey: true,
  },
};

// Helper function to get provider info with fallback
export function getProviderInfo(providerId: string): ProviderInfo {
  return (
    PROVIDERS[providerId] || {
      id: providerId,
      name: providerId,
      color: 'bg-gray-500/10 text-gray-600 dark:text-gray-400',
      icon: (className = 'h-5 w-5') => <Brain className={className} />,
      requiresApiKey: true,
    }
  );
}

// Get list of providers for dropdowns (excluding duplicates like 'google' and 'grok')
export function getProviderList(): ProviderInfo[] {
  return Object.values(PROVIDERS).filter(
    provider => provider.id !== 'google' && provider.id !== 'grok'
  );
}
