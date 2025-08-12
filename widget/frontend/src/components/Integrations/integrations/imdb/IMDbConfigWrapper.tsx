import { ApiKeyConfig } from '@/components/ApiKeyConfig/ApiKeyConfig';

interface IMDbConfigWrapperProps {
  onClose: () => void;
  onSuccess?: () => void;
}

export function IMDbConfigWrapper({ onClose, onSuccess }: IMDbConfigWrapperProps) {
  const handleConfigured = () => {
    // Mark as configured in localStorage for backward compatibility
    localStorage.setItem('imdb_configured', 'true');
    onSuccess?.();
    onClose();
  };

  return (
    <div className="space-y-4">
      <ApiKeyConfig
        service="imdb"
        displayName="OMDb API"
        description="Enter your OMDb API key to enable movie and TV show searches"
        keyName="api_key"
        onConfigured={handleConfigured}
      />
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Get a free API key from{' '}
        <a
          href="http://www.omdbapi.com/apikey.aspx"
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-500 dark:text-blue-400 underline"
        >
          OMDb API website
        </a>
      </p>
    </div>
  );
}
