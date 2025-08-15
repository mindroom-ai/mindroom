import { 
  SiOpenai, 
  SiAnthropic,
  SiGoogle,
} from 'react-icons/si';
import { 
  FaServer, 
  FaGlobe,
  FaBook,
  FaRocket,
  FaCode,
  FaUsers,
  FaBrain,
  FaCloud,
} from 'react-icons/fa';
import { BsLightningChargeFill } from 'react-icons/bs';
import { TbWorldSearch } from 'react-icons/tb';

interface ProviderLogoProps {
  provider: string;
  className?: string;
}

export function ProviderLogo({ provider, className = 'h-5 w-5' }: ProviderLogoProps) {
  // Map providers to appropriate icons from react-icons
  const providerIcons: Record<string, JSX.Element> = {
    // Use official brand icons where available
    openai: <SiOpenai className={className} />,
    anthropic: <SiAnthropic className={className} />,
    google: <SiGoogle className={className} />,
    gemini: <SiGoogle className={className} />,  // Google's Gemini
    
    // Use representative icons for others
    ollama: <FaServer className={className} />,          // Local server
    openrouter: <FaGlobe className={className} />,        // Global routing
    groq: <BsLightningChargeFill className={className} />, // Fast inference
    deepseek: <FaCode className={className} />,           // Coding focused
    together: <FaUsers className={className} />,          // Community/together
    mistral: <FaCloud className={className} />,           // Cloud AI
    perplexity: <TbWorldSearch className={className} />,  // Search focused
    cohere: <FaBook className={className} />,             // Text/embeddings
    xai: <FaRocket className={className} />,              // Grok/xAI
  };

  // Default icon for unknown providers
  return providerIcons[provider] || <FaBrain className={className} />;
}