// Google icon inline to avoid react-icons import
import { Integration, IntegrationProvider, IntegrationConfig } from '../types';
import { GoogleIntegration as GoogleIntegrationComponent } from '@/components/GoogleIntegration/GoogleIntegration';

// Wrapper component to handle the dialog integration
function GoogleConfigDialog(props: { onClose: () => void; onSuccess?: () => void }) {
  // Pass the onSuccess callback to the GoogleIntegrationComponent
  return <GoogleIntegrationComponent onSuccess={props.onSuccess} />;
}

class GoogleIntegrationProvider implements IntegrationProvider {
  private integration: Integration = {
    id: 'google',
    name: 'Google Services',
    description: 'Gmail, Calendar, and Drive integration',
    category: 'email',
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" className="h-5 w-5">
        <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
        <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
        <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
        <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
      </svg>
    ),
    status: 'available',
    setup_type: 'special',
    connected: false,
  };

  getConfig(): IntegrationConfig {
    return {
      integration: this.integration,
      onAction: async () => {
        // The parent component will handle showing the dialog
        // This is handled via the ConfigComponent
      },
      onDisconnect: async () => {
        const response = await fetch('/api/google/disconnect', {
          method: 'POST',
        });
        if (!response.ok) {
          throw new Error('Failed to disconnect Google services');
        }
      },
      ConfigComponent: GoogleConfigDialog,
      checkConnection: this.checkConnection.bind(this),
    };
  }

  async loadStatus(): Promise<Partial<Integration>> {
    try {
      const response = await fetch('/api/google/status');
      if (response.ok) {
        const data = await response.json();
        if (data.connected) {
          return {
            status: 'connected',
            connected: true,
          };
        }
      }
    } catch (error) {
      console.error('Failed to load Google status:', error);
    }
    return {
      status: 'available',
      connected: false,
    };
  }

  private async checkConnection(): Promise<boolean> {
    try {
      const response = await fetch('/api/google/status');
      if (response.ok) {
        const data = await response.json();
        return data.connected === true;
      }
    } catch (error) {
      console.error('Failed to check Google connection:', error);
    }
    return false;
  }
}

export const googleIntegration = new GoogleIntegrationProvider();
