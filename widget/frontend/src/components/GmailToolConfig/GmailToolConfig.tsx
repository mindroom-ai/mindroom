import { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Badge } from '@/components/ui/badge';
import { useToast } from '@/components/ui/use-toast';
import {
  Mail,
  CheckCircle2,
  Settings,
  Wand2,
  Key,
  Loader2,
  ExternalLink,
  Copy,
} from 'lucide-react';
import { API_BASE } from '@/lib/api';

interface GmailStatus {
  configured: boolean;
  method?: 'oauth' | 'manual' | null;
  email?: string;
  hasCredentials: boolean;
}

export function GmailToolConfig() {
  const [status, setStatus] = useState<GmailStatus>({
    configured: false,
    hasCredentials: false,
  });
  const [loading, setLoading] = useState(false);
  const [manualClientId, setManualClientId] = useState('');
  const [manualClientSecret, setManualClientSecret] = useState('');
  const { toast } = useToast();

  useEffect(() => {
    checkGmailStatus();
  }, []);

  const checkGmailStatus = async () => {
    try {
      // Check if Google credentials are configured
      const response = await fetch(`${API_BASE}/api/gmail/status`);
      if (response.ok) {
        const data = await response.json();
        setStatus(data);
      }
    } catch (error) {
      console.error('Failed to check Gmail status:', error);
    }
  };

  const handleManualSetup = async () => {
    if (!manualClientId || !manualClientSecret) {
      toast({
        title: 'Missing Credentials',
        description: 'Please enter both Client ID and Client Secret',
        variant: 'destructive',
      });
      return;
    }

    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/gmail/configure`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          client_id: manualClientId,
          client_secret: manualClientSecret,
          method: 'manual',
        }),
      });

      if (response.ok) {
        toast({
          title: 'Success!',
          description: 'Gmail credentials saved. Agents can now use Gmail.',
        });
        await checkGmailStatus();
        setManualClientId('');
        setManualClientSecret('');
      } else {
        throw new Error('Failed to save credentials');
      }
    } catch (error) {
      toast({
        title: 'Error',
        description: 'Failed to save Gmail credentials',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleAutoSetup = async () => {
    setLoading(true);
    try {
      // Start OAuth flow
      const response = await fetch(`${API_BASE}/api/gmail/oauth/start`, {
        method: 'POST',
      });

      if (response.ok) {
        const data = await response.json();

        // Check if credentials are needed first
        if (data.needs_credentials) {
          toast({
            title: 'Setup Required',
            description:
              data.message ||
              'Please use the Manual Setup tab to configure Google OAuth credentials first.',
            variant: 'destructive',
          });
          setLoading(false);
          return;
        }

        // Open OAuth window if we have an auth URL
        if (data.auth_url) {
          const authWindow = window.open(data.auth_url, '_blank', 'width=500,height=600');

          // Poll for completion
          const pollInterval = setInterval(async () => {
            await checkGmailStatus();
            if (authWindow?.closed) {
              clearInterval(pollInterval);
              setLoading(false);
            }
          }, 2000);
        }
      } else {
        const errorData = await response.json().catch(() => ({}));
        toast({
          title: 'Error',
          description: errorData.detail || 'Failed to start OAuth flow',
          variant: 'destructive',
        });
        setLoading(false);
      }
    } catch (error) {
      toast({
        title: 'Error',
        description: 'Failed to start OAuth flow',
        variant: 'destructive',
      });
      setLoading(false);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    toast({
      title: 'Copied!',
      description: 'Copied to clipboard',
    });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Mail className="h-5 w-5" />
          Gmail Tool Configuration
        </CardTitle>
        <CardDescription>
          Enable Gmail access for your AI agents. Choose automatic setup or provide your own API
          keys.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {status.configured ? (
          <div className="space-y-4">
            <Alert className="bg-green-50 dark:bg-green-950">
              <CheckCircle2 className="h-4 w-4 text-green-600" />
              <AlertTitle>Gmail Configured</AlertTitle>
              <AlertDescription>
                <div className="mt-2 space-y-1">
                  <p>
                    Setup method: <Badge variant="secondary">{status.method}</Badge>
                  </p>
                  {status.email && <p>Connected account: {status.email}</p>}
                </div>
              </AlertDescription>
            </Alert>

            <div className="p-4 bg-muted rounded-lg space-y-2">
              <p className="font-medium">Your agents can now:</p>
              <ul className="text-sm space-y-1 ml-4">
                <li>✓ Read and search emails</li>
                <li>✓ Send emails on your behalf</li>
                <li>✓ Create drafts</li>
                <li>✓ Manage inbox and labels</li>
              </ul>
            </div>

            <Button
              variant="outline"
              onClick={() => setStatus({ configured: false, hasCredentials: false })}
              className="w-full"
            >
              Reconfigure
            </Button>
          </div>
        ) : (
          <Tabs defaultValue="automatic" className="space-y-4">
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="automatic">
                <Wand2 className="mr-2 h-4 w-4" />
                Automatic Setup
              </TabsTrigger>
              <TabsTrigger value="manual">
                <Key className="mr-2 h-4 w-4" />
                Manual Setup
              </TabsTrigger>
            </TabsList>

            <TabsContent value="automatic" className="space-y-4">
              <Alert>
                <Wand2 className="h-4 w-4" />
                <AlertTitle>Easiest Option</AlertTitle>
                <AlertDescription>
                  We'll guide you through Google's OAuth flow. No need to create API keys manually.
                </AlertDescription>
              </Alert>

              <div className="space-y-3">
                <div className="p-3 bg-muted rounded">
                  <p className="font-medium mb-2">How it works:</p>
                  <ol className="text-sm space-y-1 ml-4 text-muted-foreground">
                    <li>1. Click the button below</li>
                    <li>2. Sign in with Google</li>
                    <li>3. Grant permissions to MindRoom</li>
                    <li>4. Done! Your agents can use Gmail</li>
                  </ol>
                </div>

                <Button onClick={handleAutoSetup} disabled={loading} className="w-full" size="lg">
                  {loading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Setting up...
                    </>
                  ) : (
                    <>
                      <img
                        src="https://www.google.com/favicon.ico"
                        alt="Google"
                        className="mr-2 h-4 w-4"
                      />
                      Setup with Google
                    </>
                  )}
                </Button>
              </div>
            </TabsContent>

            <TabsContent value="manual" className="space-y-4">
              <Alert>
                <Key className="h-4 w-4" />
                <AlertTitle>Use Your Own API Keys</AlertTitle>
                <AlertDescription>
                  If you already have Google Cloud credentials or prefer to create your own.
                </AlertDescription>
              </Alert>

              <div className="space-y-4">
                <div className="p-3 bg-muted rounded">
                  <p className="font-medium mb-2">Get your credentials:</p>
                  <ol className="text-sm space-y-2 text-muted-foreground">
                    <li className="flex items-start gap-2">
                      <span>1.</span>
                      <div className="flex-1">
                        Go to{' '}
                        <Button
                          variant="link"
                          size="sm"
                          className="h-auto p-0"
                          onClick={() => window.open('https://console.cloud.google.com', '_blank')}
                        >
                          Google Cloud Console <ExternalLink className="ml-1 h-3 w-3" />
                        </Button>
                      </div>
                    </li>
                    <li>2. Create a project (or use existing)</li>
                    <li>3. Enable Gmail API</li>
                    <li>4. Create OAuth 2.0 credentials</li>
                    <li>5. Copy Client ID and Secret below</li>
                  </ol>
                </div>

                <div className="space-y-3">
                  <div>
                    <Label htmlFor="client-id">Client ID</Label>
                    <div className="flex gap-2">
                      <Input
                        id="client-id"
                        placeholder="your-client-id.apps.googleusercontent.com"
                        value={manualClientId}
                        onChange={e => setManualClientId(e.target.value)}
                      />
                      {manualClientId && (
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => copyToClipboard(manualClientId)}
                        >
                          <Copy className="h-4 w-4" />
                        </Button>
                      )}
                    </div>
                  </div>

                  <div>
                    <Label htmlFor="client-secret">Client Secret</Label>
                    <Input
                      id="client-secret"
                      type="password"
                      placeholder="your-client-secret"
                      value={manualClientSecret}
                      onChange={e => setManualClientSecret(e.target.value)}
                    />
                  </div>

                  <Button
                    onClick={handleManualSetup}
                    disabled={!manualClientId || !manualClientSecret || loading}
                    className="w-full"
                  >
                    {loading ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Saving...
                      </>
                    ) : (
                      <>
                        <Settings className="mr-2 h-4 w-4" />
                        Save Credentials
                      </>
                    )}
                  </Button>
                </div>

                <div className="text-xs text-muted-foreground">
                  <p>
                    Need help? Check our{' '}
                    <Button
                      variant="link"
                      size="sm"
                      className="h-auto p-0 text-xs"
                      onClick={() => window.open('/docs/gmail_setup.md', '_blank')}
                    >
                      setup guide
                    </Button>
                  </p>
                </div>
              </div>
            </TabsContent>
          </Tabs>
        )}
      </CardContent>
    </Card>
  );
}
