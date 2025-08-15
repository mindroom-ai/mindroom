import { SiOpenai, SiAnthropic, SiGoogle, SiPerplexity } from 'react-icons/si';
import { FaBrain } from 'react-icons/fa';
import { Ollama, OpenRouter, Groq, DeepSeek, Together, Mistral, Cohere, XAI } from '@lobehub/icons';

interface ProviderLogoProps {
  provider: string;
  className?: string;
}

export function ProviderLogo({ provider, className = 'h-5 w-5' }: ProviderLogoProps) {
  // Normalize provider name to lowercase for comparison
  const normalizedProvider = provider?.toLowerCase();

  // Map providers to appropriate icons
  const providerIcons: Record<string, JSX.Element> = {
    // Use official brand icons where available
    openai: <SiOpenai className={className} />,
    anthropic: <SiAnthropic className={className} />,
    google: <SiGoogle className={className} />,
    gemini: <SiGoogle className={className} />, // Google's Gemini

    // Use Lobe Icons for these providers
    ollama: <Ollama className={className} />,
    openrouter: <OpenRouter className={className} />,
    groq: <Groq className={className} />,
    deepseek: <DeepSeek className={className} />,
    together: <Together className={className} />,
    mistral: <Mistral className={className} />,
    cohere: <Cohere className={className} />,
    xai: <XAI className={className} />,
    grok: <XAI className={className} />, // Alternative name for xAI

    // Official Perplexity icon from react-icons
    perplexity: <SiPerplexity className={className} />,
  };

  // Default icon for unknown providers
  return providerIcons[normalizedProvider] || <FaBrain className={className} />;
}
