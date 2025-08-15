interface ProviderLogoProps {
  provider: string;
  className?: string;
}

export function ProviderLogo({ provider, className = 'h-5 w-5' }: ProviderLogoProps) {
  const logos: Record<string, JSX.Element> = {
    openai: (
      <svg viewBox="0 0 24 24" className={className} fill="currentColor">
        <path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.975 5.975 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-3.83-2.213A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855l-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l3.83 2.213a4.494 4.494 0 0 1-.419 8.039v-5.678a.78.78 0 0 0-.401-.675z"/>
      </svg>
    ),
    anthropic: (
      <svg viewBox="0 0 24 24" className={className} fill="currentColor">
        <path d="M17.335 3.5l-5.01 17h3.092l1.162-3.932h5.61L23.335 20.5h3.17l-5.01-17h-4.16zm1.947 10.5h-3.915l1.957-6.628L19.282 14zM.5 20.5h3.092l5.01-17h-3.09l-5.012 17z"/>
      </svg>
    ),
    ollama: (
      <svg viewBox="0 0 24 24" className={className} fill="currentColor">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm2.59-11.41L16 10l-4 4-4-4 1.41-1.41L11 10.17V6h2v4.17l1.59-1.58z"/>
      </svg>
    ),
    openrouter: (
      <svg viewBox="0 0 24 24" className={className} fill="currentColor">
        <path d="M12 2L2 7v10c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V7l-10-5zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V8.93l7-3.11v7.17z"/>
      </svg>
    ),
    gemini: (
      <svg viewBox="0 0 24 24" className={className} fill="currentColor">
        <path d="M12 0C12 6.62742 6.62742 12 0 12C6.62742 12 12 17.3726 12 24C12 17.3726 17.3726 12 24 12C17.3726 12 12 6.62742 12 0Z"/>
      </svg>
    ),
    google: (
      <svg viewBox="0 0 24 24" className={className} fill="currentColor">
        <path d="M12 0C12 6.62742 6.62742 12 0 12C6.62742 12 12 17.3726 12 24C12 17.3726 17.3726 12 24 12C17.3726 12 12 6.62742 12 0Z"/>
      </svg>
    ),
  };

  return logos[provider] || (
    <div className={className}>
      <svg viewBox="0 0 24 24" className="h-full w-full" fill="currentColor">
        <circle cx="12" cy="12" r="10" opacity="0.3"/>
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/>
      </svg>
    </div>
  );
}