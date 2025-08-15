import { useState, useEffect } from 'react';
import { Mail, Calendar, HardDrive, CheckCircle2, XCircle, Loader2, LogIn } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { useToast } from '@/components/ui/use-toast';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { API_BASE } from '@/lib/api';

interface GoogleStatus {
  connected: boolean;
  email?: string;
  services: string[];
  error?: string;
}

const serviceIcons = {
  Gmail: Mail,
  'Google Calendar': Calendar,
  'Google Drive': HardDrive,
};

interface GoogleIntegrationProps {
  onSuccess?: () => void;
}

export function GoogleIntegration({ onSuccess }: GoogleIntegrationProps = {}) {
  const [status, setStatus] = useState<GoogleStatus>({ connected: false, services: [] });
  const [loading, setLoading] = useState(false);
  const { toast } = useToast();

  // Check Google connection status on mount
  useEffect(() => {
    checkGoogleStatus();

    // Check if we're returning from OAuth callback
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('google') === 'connected') {
      // Remove the query parameter
      window.history.replaceState({}, document.title, window.location.pathname);
      // Refresh status
      checkGoogleStatus();
      toast({
        title: 'Google Account Connected',
        description: 'Your Google services are now available to MindRoom agents.',
      });

      // Notify parent component of success
      if (onSuccess) {
        onSuccess();
      }
    }
  }, []);

  const checkGoogleStatus = async () => {
    try {
      const response = await fetch(`${API_BASE}/api/google/status`);
      const data = await response.json();
      setStatus({
        connected: data.connected,
        email: data.email,
        services: data.services || [],
      });
      return data.connected;
    } catch (error) {
      console.error('Failed to check Google status:', error);
      return false;
    }
  };

  const connectGoogle = async () => {
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/google/connect`, {
        method: 'POST',
      });

      if (!response.ok) {
        const error = await response.json();

        // Check if it's a configuration issue
        if (response.status === 503) {
          toast({
            title: 'One-Time Admin Setup Required',
            description:
              'An administrator needs to complete a quick 5-minute setup for all users. See docs/google_oauth_setup_admin.md',
            variant: 'destructive',
          });
          setLoading(false);
          return;
        }

        throw new Error(error.detail || 'Failed to start Google authentication');
      }

      const data = await response.json();

      // Check if credentials need to be configured first
      if (data.needs_credentials) {
        toast({
          title: 'Setup Required',
          description: data.message || 'Please configure Google OAuth credentials first.',
          variant: 'destructive',
        });
        setLoading(false);
        return;
      }

      // Open OAuth URL in new window
      const authWindow = window.open(data.auth_url, '_blank', 'width=500,height=600');

      // Poll for connection status
      let wasConnected = status.connected;
      const pollInterval = setInterval(async () => {
        const currentStatus = await checkGoogleStatus();
        if (authWindow?.closed) {
          clearInterval(pollInterval);
          setLoading(false);
          // If we're now connected and weren't before, call onSuccess
          if (!wasConnected && currentStatus && onSuccess) {
            onSuccess();
          }
        }
      }, 2000);
    } catch (error) {
      console.error('Failed to connect Google:', error);
      toast({
        title: 'Connection Failed',
        description: error instanceof Error ? error.message : 'Failed to connect to Google',
        variant: 'destructive',
      });
      setLoading(false);
    }
  };

  const disconnectGoogle = async () => {
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/google/disconnect`, {
        method: 'POST',
      });

      if (response.ok) {
        setStatus({ connected: false, services: [] });
        toast({
          title: 'Disconnected',
          description: 'Your Google account has been disconnected.',
        });
        // Notify parent component to refresh
        if (onSuccess) {
          onSuccess();
        }
      }
    } catch (error) {
      console.error('Failed to disconnect:', error);
      toast({
        title: 'Error',
        description: 'Failed to disconnect Google account.',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <img src="https://www.google.com/favicon.ico" alt="Google" className="w-5 h-5" />
          Google Services Integration
        </CardTitle>
        <CardDescription>
          One-click connection to Gmail, Calendar, and Drive - no technical setup required!
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {!status.connected ? (
          <>
            <Alert>
              <LogIn className="h-4 w-4" />
              <AlertTitle>Zero Setup for Users!</AlertTitle>
              <AlertDescription>
                Just click "Login with Google" below. No API keys, no Google Cloud Console, no
                developer account needed! If this button doesn't work, your admin needs to do a
                quick 5-minute setup that will work for all users.
              </AlertDescription>
            </Alert>

            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">This will grant MindRoom access to:</p>
              <ul className="text-sm space-y-1 ml-4">
                <li className="flex items-center gap-2">
                  <Mail className="h-3 w-3" /> Gmail - Read, compose, and send emails
                </li>
                <li className="flex items-center gap-2">
                  <Calendar className="h-3 w-3" /> Calendar - Manage events and schedules
                </li>
                <li className="flex items-center gap-2">
                  <HardDrive className="h-3 w-3" /> Drive - Access and manage files
                </li>
              </ul>
            </div>

            <Button onClick={connectGoogle} disabled={loading} className="w-full" size="lg">
              {loading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Connecting...
                </>
              ) : (
                <>
                  <img
                    src="https://www.google.com/favicon.ico"
                    alt="Google"
                    className="mr-2 h-4 w-4"
                  />
                  Login with Google
                </>
              )}
            </Button>
          </>
        ) : (
          <>
            <div className="flex items-center justify-between p-3 bg-green-50 dark:bg-green-950 rounded-lg">
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-5 w-5 text-green-600" />
                <div>
                  <p className="font-medium">Connected</p>
                  {status.email && <p className="text-sm text-muted-foreground">{status.email}</p>}
                </div>
              </div>
            </div>

            <div className="space-y-2">
              <p className="text-sm font-medium">Active Services:</p>
              <div className="flex flex-wrap gap-2">
                {status.services.map(service => {
                  const Icon = serviceIcons[service as keyof typeof serviceIcons] || Mail;
                  return (
                    <Badge key={service} variant="secondary" className="flex items-center gap-1">
                      <Icon className="h-3 w-3" />
                      {service}
                    </Badge>
                  );
                })}
              </div>
            </div>

            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">Your agents can now:</p>
              <ul className="text-sm space-y-1 ml-4 text-muted-foreground">
                <li>• Read and search your emails</li>
                <li>• Send emails on your behalf</li>
                <li>• Manage calendar events</li>
                <li>• Access Google Drive files</li>
              </ul>
            </div>

            <Button
              onClick={disconnectGoogle}
              disabled={loading}
              variant="outline"
              className="w-full"
            >
              {loading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Disconnecting...
                </>
              ) : (
                'Disconnect Google Account'
              )}
            </Button>
          </>
        )}

        {status.error && (
          <Alert variant="destructive">
            <XCircle className="h-4 w-4" />
            <AlertTitle>Error</AlertTitle>
            <AlertDescription>{status.error}</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
