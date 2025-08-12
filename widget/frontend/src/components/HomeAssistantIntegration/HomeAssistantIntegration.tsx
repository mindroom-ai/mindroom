import { useState, useEffect } from 'react';
import {
  Home,
  Loader2,
  CheckCircle2,
  Key,
  Link2,
  Lightbulb,
  Thermometer,
  Shield,
  Activity,
  Info,
  RefreshCw,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useToast } from '@/components/ui/use-toast';
import { API_BASE } from '@/lib/api';

interface HomeAssistantStatus {
  connected: boolean;
  instance_url?: string;
  version?: string;
  location_name?: string;
  error?: string;
  has_credentials: boolean;
  entities_count: number;
}

interface HomeAssistantIntegrationProps {
  onSuccess?: () => void;
}

export function HomeAssistantIntegration({ onSuccess }: HomeAssistantIntegrationProps = {}) {
  const [status, setStatus] = useState<HomeAssistantStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [instanceUrl, setInstanceUrl] = useState('');
  const [clientId, setClientId] = useState('');
  const [longLivedToken, setLongLivedToken] = useState('');
  const [activeTab, setActiveTab] = useState('oauth');
  const { toast } = useToast();

  useEffect(() => {
    checkStatus();
  }, []);

  const checkStatus = async () => {
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/homeassistant/status`);
      if (response.ok) {
        const data = await response.json();
        setStatus(data);
      }
    } catch (error) {
      console.error('Failed to check Home Assistant status:', error);
    } finally {
      setLoading(false);
    }
  };

  const connectOAuth = async () => {
    if (!instanceUrl) {
      toast({
        title: 'Missing Information',
        description: 'Please enter your Home Assistant instance URL',
        variant: 'destructive',
      });
      return;
    }

    if (!clientId) {
      toast({
        title: 'Missing Information',
        description: 'Please enter your OAuth Client ID',
        variant: 'destructive',
      });
      return;
    }

    setConnecting(true);
    try {
      const response = await fetch(`${API_BASE}/api/homeassistant/connect/oauth`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          instance_url: instanceUrl,
          client_id: clientId,
        }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to start OAuth flow');
      }

      const data = await response.json();

      // Open OAuth window
      const authWindow = window.open(data.auth_url, '_blank', 'width=600,height=700');

      // Poll for completion
      const pollInterval = setInterval(async () => {
        if (authWindow?.closed) {
          clearInterval(pollInterval);
          setConnecting(false);

          // Check if connection was successful
          const previousStatus = status?.connected;
          await checkStatus();

          // Check the status again to see if we're now connected
          const statusResponse = await fetch(`${API_BASE}/api/homeassistant/status`);
          if (statusResponse.ok) {
            const newStatus = await statusResponse.json();
            if (newStatus.connected && !previousStatus) {
              // Connection was successful
              toast({
                title: 'Success!',
                description: 'Successfully connected to Home Assistant',
              });

              if (onSuccess) {
                onSuccess();
              }
            }
          }
        }
      }, 2000);
    } catch (error) {
      console.error('Failed to connect via OAuth:', error);
      toast({
        title: 'Connection Failed',
        description: error instanceof Error ? error.message : 'Failed to connect to Home Assistant',
        variant: 'destructive',
      });
      setConnecting(false);
    }
  };

  const connectToken = async () => {
    if (!instanceUrl) {
      toast({
        title: 'Missing Information',
        description: 'Please enter your Home Assistant instance URL',
        variant: 'destructive',
      });
      return;
    }

    if (!longLivedToken) {
      toast({
        title: 'Missing Information',
        description: 'Please enter your long-lived access token',
        variant: 'destructive',
      });
      return;
    }

    setConnecting(true);
    try {
      const response = await fetch(`${API_BASE}/api/homeassistant/connect/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          instance_url: instanceUrl,
          long_lived_token: longLivedToken,
        }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to connect');
      }

      toast({
        title: 'Success!',
        description: 'Successfully connected to Home Assistant',
      });

      await checkStatus();

      // Clear form
      setInstanceUrl('');
      setLongLivedToken('');

      // Notify parent component of success
      if (onSuccess) {
        onSuccess();
      }
    } catch (error) {
      console.error('Failed to connect with token:', error);
      toast({
        title: 'Connection Failed',
        description: error instanceof Error ? error.message : 'Failed to connect to Home Assistant',
        variant: 'destructive',
      });
    } finally {
      setConnecting(false);
    }
  };

  const disconnect = async () => {
    setConnecting(true);
    try {
      const response = await fetch(`${API_BASE}/api/homeassistant/disconnect`, {
        method: 'POST',
      });

      if (!response.ok) {
        throw new Error('Failed to disconnect');
      }

      toast({
        title: 'Disconnected',
        description: 'Home Assistant has been disconnected',
      });

      await checkStatus();
    } catch (error) {
      console.error('Failed to disconnect:', error);
      toast({
        title: 'Error',
        description: 'Failed to disconnect from Home Assistant',
        variant: 'destructive',
      });
    } finally {
      setConnecting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    );
  }

  if (status?.connected) {
    return (
      <div className="space-y-6">
        {/* Connection Status */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Home className="h-5 w-5 text-blue-500" />
                <CardTitle>Connection Status</CardTitle>
              </div>
              <Badge className="bg-green-500/10 text-green-700 dark:text-green-300">
                <CheckCircle2 className="h-3 w-3 mr-1" />
                Connected
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <Label className="text-sm text-gray-500">Instance URL</Label>
                <p className="font-medium">{status.instance_url}</p>
              </div>
              <div>
                <Label className="text-sm text-gray-500">Location</Label>
                <p className="font-medium">{status.location_name || 'Unknown'}</p>
              </div>
              <div>
                <Label className="text-sm text-gray-500">Version</Label>
                <p className="font-medium">{status.version || 'Unknown'}</p>
              </div>
              <div>
                <Label className="text-sm text-gray-500">Total Entities</Label>
                <p className="font-medium">{status.entities_count}</p>
              </div>
            </div>

            <div className="flex gap-2">
              <Button onClick={checkStatus} variant="outline" size="sm">
                <RefreshCw className="h-4 w-4 mr-2" />
                Refresh
              </Button>
              <Button onClick={disconnect} variant="destructive" size="sm" disabled={connecting}>
                {connecting ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Disconnect'}
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* Capabilities */}
        <Card>
          <CardHeader>
            <CardTitle>Available Capabilities</CardTitle>
            <CardDescription>
              Your agents can now control and monitor your Home Assistant devices
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3">
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-green-500" />
                <span>Control lights, switches, and other devices</span>
              </div>
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-green-500" />
                <span>Monitor sensor values and device states</span>
              </div>
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-green-500" />
                <span>Execute scenes and automations</span>
              </div>
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-green-500" />
                <span>Adjust climate controls</span>
              </div>
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-green-500" />
                <span>Query device history and statistics</span>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Setup Instructions */}
      <Alert>
        <Info className="h-4 w-4" />
        <AlertTitle>Setup Home Assistant Integration</AlertTitle>
        <AlertDescription>
          Choose your preferred authentication method below. OAuth is recommended for better
          security, but you can also use a long-lived access token for simpler setup.
        </AlertDescription>
      </Alert>

      {/* Connection Methods */}
      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="oauth">OAuth (Recommended)</TabsTrigger>
          <TabsTrigger value="token">Access Token</TabsTrigger>
        </TabsList>

        <TabsContent value="oauth" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>OAuth Authentication</CardTitle>
              <CardDescription>
                Secure authentication using Home Assistant's OAuth flow
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <Alert>
                <Info className="h-4 w-4" />
                <AlertDescription>
                  <strong>Prerequisites:</strong>
                  <ol className="mt-2 ml-4 list-decimal space-y-1 text-sm">
                    <li>
                      Create an OAuth application in Home Assistant:
                      <br />
                      Go to Profile → Security → Long-Lived Access Tokens → Add OAuth Application
                    </li>
                    <li>
                      Set the redirect URI to:{' '}
                      <code className="text-xs">http://localhost:5173/homeassistant-callback</code>
                    </li>
                    <li>Copy the Client ID from the created application</li>
                  </ol>
                </AlertDescription>
              </Alert>

              <div className="space-y-2">
                <Label htmlFor="instance-url-oauth">Home Assistant URL</Label>
                <Input
                  id="instance-url-oauth"
                  type="url"
                  placeholder="http://homeassistant.local:8123"
                  value={instanceUrl}
                  onChange={e => setInstanceUrl(e.target.value)}
                />
                <p className="text-xs text-gray-500">
                  The URL of your Home Assistant instance (including port if needed)
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="client-id">OAuth Client ID</Label>
                <Input
                  id="client-id"
                  type="text"
                  placeholder="Enter your OAuth Client ID"
                  value={clientId}
                  onChange={e => setClientId(e.target.value)}
                />
                <p className="text-xs text-gray-500">
                  The Client ID from your Home Assistant OAuth application
                </p>
              </div>

              <Button
                onClick={connectOAuth}
                disabled={!instanceUrl || !clientId || connecting}
                className="w-full"
              >
                {connecting ? (
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                ) : (
                  <Link2 className="h-4 w-4 mr-2" />
                )}
                Connect with OAuth
              </Button>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="token" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Long-Lived Access Token</CardTitle>
              <CardDescription>
                Simple authentication using a long-lived access token
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <Alert>
                <Info className="h-4 w-4" />
                <AlertDescription>
                  <strong>How to get a token:</strong>
                  <ol className="mt-2 ml-4 list-decimal space-y-1 text-sm">
                    <li>
                      Go to your Home Assistant profile (click your username at the bottom left)
                    </li>
                    <li>Scroll down to "Long-Lived Access Tokens"</li>
                    <li>Click "Create Token"</li>
                    <li>Give it a name (e.g., "MindRoom")</li>
                    <li>Copy the generated token</li>
                  </ol>
                </AlertDescription>
              </Alert>

              <div className="space-y-2">
                <Label htmlFor="instance-url-token">Home Assistant URL</Label>
                <Input
                  id="instance-url-token"
                  type="url"
                  placeholder="http://homeassistant.local:8123"
                  value={instanceUrl}
                  onChange={e => setInstanceUrl(e.target.value)}
                />
                <p className="text-xs text-gray-500">
                  The URL of your Home Assistant instance (including port if needed)
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="token">Access Token</Label>
                <Input
                  id="token"
                  type="password"
                  placeholder="Enter your long-lived access token"
                  value={longLivedToken}
                  onChange={e => setLongLivedToken(e.target.value)}
                />
                <p className="text-xs text-gray-500">
                  The long-lived access token from Home Assistant
                </p>
              </div>

              <Button
                onClick={connectToken}
                disabled={!instanceUrl || !longLivedToken || connecting}
                className="w-full"
              >
                {connecting ? (
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                ) : (
                  <Key className="h-4 w-4 mr-2" />
                )}
                Connect with Token
              </Button>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* What's Possible */}
      <Card>
        <CardHeader>
          <CardTitle>What Your Agents Can Do</CardTitle>
          <CardDescription>
            Once connected, your AI agents will be able to interact with your smart home
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <h4 className="font-medium flex items-center gap-2">
                <Lightbulb className="h-4 w-4 text-yellow-500" />
                Device Control
              </h4>
              <ul className="text-sm text-gray-600 dark:text-gray-400 space-y-1 ml-6">
                <li>• Turn lights on/off</li>
                <li>• Adjust brightness and colors</li>
                <li>• Control switches and outlets</li>
                <li>• Activate scenes</li>
              </ul>
            </div>
            <div className="space-y-2">
              <h4 className="font-medium flex items-center gap-2">
                <Thermometer className="h-4 w-4 text-blue-500" />
                Climate Control
              </h4>
              <ul className="text-sm text-gray-600 dark:text-gray-400 space-y-1 ml-6">
                <li>• Adjust thermostats</li>
                <li>• Set temperature schedules</li>
                <li>• Control HVAC modes</li>
                <li>• Monitor energy usage</li>
              </ul>
            </div>
            <div className="space-y-2">
              <h4 className="font-medium flex items-center gap-2">
                <Activity className="h-4 w-4 text-green-500" />
                Monitoring
              </h4>
              <ul className="text-sm text-gray-600 dark:text-gray-400 space-y-1 ml-6">
                <li>• Check sensor values</li>
                <li>• Monitor door/window states</li>
                <li>• Track motion detection</li>
                <li>• View device history</li>
              </ul>
            </div>
            <div className="space-y-2">
              <h4 className="font-medium flex items-center gap-2">
                <Shield className="h-4 w-4 text-red-500" />
                Security
              </h4>
              <ul className="text-sm text-gray-600 dark:text-gray-400 space-y-1 ml-6">
                <li>• Arm/disarm alarms</li>
                <li>• Check lock status</li>
                <li>• View camera feeds*</li>
                <li>• Trigger automations</li>
              </ul>
            </div>
          </div>
          <p className="text-xs text-gray-500 mt-4">
            * Camera feed access requires additional configuration
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
