import { ReactElement } from 'react';
// Import just the Mono (simple) variant of each icon to avoid heavy dependencies
import Ollama from '@lobehub/icons/es/Ollama/components/Mono';
import OpenRouter from '@lobehub/icons/es/OpenRouter/components/Mono';
import Groq from '@lobehub/icons/es/Groq/components/Mono';
import DeepSeek from '@lobehub/icons/es/DeepSeek/components/Mono';
import Together from '@lobehub/icons/es/Together/components/Mono';
import Mistral from '@lobehub/icons/es/Mistral/components/Mono';
import Cohere from '@lobehub/icons/es/Cohere/components/Mono';
import XAI from '@lobehub/icons/es/XAI/components/Mono';
import Cerebras from '@lobehub/icons/es/Cerebras/components/Mono';
import OpenAI from '@lobehub/icons/es/OpenAI/components/Mono';
import Anthropic from '@lobehub/icons/es/Anthropic/components/Mono';
import Google from '@lobehub/icons/es/Google/components/Mono';
import Perplexity from '@lobehub/icons/es/Perplexity/components/Mono';
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
    icon: (className = 'h-5 w-5') => <OpenAI className={className} />,
    requiresApiKey: true,
  },
  anthropic: {
    id: 'anthropic',
    name: 'Anthropic',
    description: 'Configure your Anthropic API key for Claude models',
    color: 'bg-purple-500/10 text-purple-600 dark:text-purple-400 border-purple-500/20',
    icon: (className = 'h-5 w-5') => <Anthropic className={className} />,
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
    icon: (className = 'h-5 w-5') => <Google className={className} />,
    requiresApiKey: true,
  },
  google: {
    id: 'google',
    name: 'Google Gemini',
    description: 'Configure your Google API key for Gemini models',
    color: 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 border-cyan-500/20',
    icon: (className = 'h-5 w-5') => <Google className={className} />,
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
    icon: (className = 'h-5 w-5') => <Perplexity className={className} />,
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
