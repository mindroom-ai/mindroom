import { getProviderInfo } from '@/lib/providers';

interface ProviderLogoProps {
  provider: string;
  className?: string;
}

export function ProviderLogo({ provider, className = 'h-5 w-5' }: ProviderLogoProps) {
  const providerInfo = getProviderInfo(provider?.toLowerCase());
  return providerInfo.icon(className);
}
