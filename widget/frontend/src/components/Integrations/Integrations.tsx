import { useState, useEffect, useMemo } from 'react';
import {
  ArrowRight,
  Settings,
  CheckCircle2,
  XCircle,
  Loader2,
  Key,
  ExternalLink,
  Star,
} from 'lucide-react';
// Brand icons from react-icons
import { FaGoogle } from 'react-icons/fa';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { useToast } from '@/components/ui/use-toast';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { GoogleIntegration } from '@/components/GoogleIntegration/GoogleIntegration';
import { API_BASE } from '@/lib/api';
import { useTools, mapToolToIntegration } from '@/hooks/useTools';
import { getIconForTool } from './iconMapping';

interface UnifiedIntegration {
  id: string;
  name: string;
  description: string;
  category: string;
  icon: React.ReactNode;
  status: 'connected' | 'not_connected' | 'available' | 'coming_soon';
  setup_type: 'oauth' | 'api_key' | 'special' | 'coming_soon';
  connected?: boolean;
  details?: any;
}

// Special handling for integrations that require custom configuration
// These override the backend metadata with special frontend requirements
const SPECIAL_INTEGRATIONS: UnifiedIntegration[] = [
  {
    id: 'google',
    name: 'Google Services',
    description: 'Gmail, Calendar, and Drive integration',
    category: 'email',
    icon: <FaGoogle className="h-5 w-5" />,
    status: 'available',
    setup_type: 'special',
  },
  // IMDb and Spotify have special frontend handling even though they're "coming soon" in backend
  {
    id: 'imdb',
    name: 'Movies & TV (IMDb)',
    description: 'Get movie and TV show information from IMDb',
    category: 'entertainment',
    icon: null, // Will be replaced by iconMapping
    status: 'available',
    setup_type: 'api_key',
  },
  {
    id: 'spotify',
    name: 'Spotify',
    description: 'Access your Spotify music data and current playback',
    category: 'entertainment',
    icon: null, // Will be replaced by iconMapping
    status: 'available',
    setup_type: 'oauth',
  },
];

export function Integrations() {
  // Fetch tools from backend
  const { tools: backendTools, loading: toolsLoading } = useTools();

  // Map backend tools to frontend format
  const toolIntegrations = useMemo(() => {
    return backendTools.map(tool => {
      const mapped = mapToolToIntegration(tool);
      return {
        ...mapped,
        icon: getIconForTool(tool.icon),
        connected: false, // Will be updated by loadServicesStatus
      } as UnifiedIntegration;
    });
  }, [backendTools]);

  // Combine with special integrations (Google)
  const [integrations, setIntegrations] = useState<UnifiedIntegration[]>([]);
  const [loading, setLoading] = useState(false);

  // Update integrations when tools are loaded
  useEffect(() => {
    if (toolIntegrations.length > 0) {
      // Start with backend tools
      let merged = [...toolIntegrations];

      // Override or add special integrations
      SPECIAL_INTEGRATIONS.forEach(special => {
        const existingIndex = merged.findIndex(ti => ti.id === special.id);
        if (existingIndex >= 0) {
          // Override existing with special configuration
          merged[existingIndex] = {
            ...merged[existingIndex],
            ...special,
            icon: special.icon || getIconForTool(merged[existingIndex].icon as string),
          };
        } else {
          // Add new special integration
          merged.push({
            ...special,
            icon: special.icon || getIconForTool(special.id),
          });
        }
      });

      setIntegrations(merged);
    }
  }, [toolIntegrations]);
  const [configDialog, setConfigDialog] = useState<{ open: boolean; service?: string }>({
    open: false,
  });
  const [googleDialog, setGoogleDialog] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const [showOnlyAvailable, setShowOnlyAvailable] = useState(false);
  const { toast } = useToast();

  useEffect(() => {
    loadServicesStatus();
  }, []);

  const loadServicesStatus = async () => {
    try {
      // Check Gmail/Google status through the actual Gmail config endpoint
      const gmailResponse = await fetch(`${API_BASE}/api/gmail/status`);
      if (gmailResponse.ok) {
        const gmailData = await gmailResponse.json();
        if (gmailData.configured) {
          setIntegrations(prev =>
            prev.map(integration =>
              integration.id === 'google'
                ? { ...integration, status: 'connected', connected: true }
                : integration
            )
          );
        }
      }

      // Check IMDb status
      const imdbCreds = localStorage.getItem('imdb_configured');
      if (imdbCreds) {
        setIntegrations(prev =>
          prev.map(integration =>
            integration.id === 'imdb'
              ? { ...integration, status: 'connected', connected: true }
              : integration
          )
        );
      }

      // Check Spotify status
      const spotifyCreds = localStorage.getItem('spotify_configured');
      if (spotifyCreds) {
        setIntegrations(prev =>
          prev.map(integration =>
            integration.id === 'spotify'
              ? { ...integration, status: 'connected', connected: true }
              : integration
          )
        );
      }
    } catch (error) {
      console.error('Failed to load services status:', error);
    }
  };

  const connectSpotify = async () => {
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/integrations/spotify/connect`, {
        method: 'POST',
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to connect Spotify');
      }

      const data = await response.json();
      const authWindow = window.open(data.auth_url, '_blank', 'width=500,height=600');

      const pollInterval = setInterval(async () => {
        if (authWindow?.closed) {
          clearInterval(pollInterval);
          setLoading(false);
          localStorage.setItem('spotify_configured', 'true');
          await loadServicesStatus();
        }
      }, 2000);
    } catch (error) {
      console.error('Failed to connect Spotify:', error);
      toast({
        title: 'Connection Failed',
        description: error instanceof Error ? error.message : 'Failed to connect Spotify',
        variant: 'destructive',
      });
      setLoading(false);
    }
  };

  const configureImdb = async () => {
    if (!apiKey) {
      toast({
        title: 'Missing API Key',
        description: 'Please enter your OMDb API key',
        variant: 'destructive',
      });
      return;
    }

    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/integrations/imdb/configure`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service: 'imdb',
          api_key: apiKey,
        }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to configure IMDb');
      }

      toast({
        title: 'Success!',
        description: 'IMDb has been configured. Agents can now search for movies and TV shows.',
      });

      localStorage.setItem('imdb_configured', 'true');
      setConfigDialog({ open: false });
      setApiKey('');
      await loadServicesStatus();
    } catch (error) {
      console.error('Failed to configure IMDb:', error);
      toast({
        title: 'Configuration Failed',
        description: error instanceof Error ? error.message : 'Failed to configure IMDb',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const disconnectService = async (serviceId: string) => {
    // Remove from localStorage
    localStorage.removeItem(`${serviceId}_configured`);

    // Update UI
    setIntegrations(prev =>
      prev.map(integration =>
        integration.id === serviceId
          ? { ...integration, status: 'available', connected: false }
          : integration
      )
    );

    toast({
      title: 'Disconnected',
      description: `${serviceId} has been disconnected.`,
    });
  };

  const handleServiceAction = (integration: UnifiedIntegration) => {
    if (integration.id === 'google') {
      setGoogleDialog(true);
      return;
    }

    if (integration.setup_type === 'coming_soon') {
      toast({
        title: 'Coming Soon',
        description: `${integration.name} integration is in development and will be available soon.`,
      });
      return;
    }

    if (integration.status === 'connected') {
      disconnectService(integration.id);
    } else if (integration.id === 'spotify') {
      connectSpotify();
    } else if (integration.id === 'imdb') {
      setConfigDialog({ open: true, service: 'imdb' });
    }
  };

  const getActionButton = (integration: UnifiedIntegration) => {
    if (integration.setup_type === 'coming_soon') {
      return (
        <Button disabled size="sm" variant="outline">
          <Star className="h-4 w-4 mr-2" />
          Coming Soon
        </Button>
      );
    }

    if (integration.status === 'connected') {
      return (
        <Button
          onClick={() => handleServiceAction(integration)}
          disabled={loading}
          variant="destructive"
          size="sm"
        >
          Disconnect
        </Button>
      );
    }

    const buttonText =
      integration.id === 'google'
        ? 'Setup'
        : integration.setup_type === 'oauth'
          ? 'Connect'
          : 'Configure';
    const icon =
      integration.id === 'google' ? (
        <Settings className="h-4 w-4" />
      ) : integration.setup_type === 'oauth' ? (
        <ExternalLink className="h-4 w-4" />
      ) : (
        <Key className="h-4 w-4" />
      );

    return (
      <Button
        onClick={() => handleServiceAction(integration)}
        disabled={loading}
        size="sm"
        className="flex items-center gap-2"
      >
        {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
        {buttonText}
      </Button>
    );
  };

  const IntegrationCard = ({ integration }: { integration: UnifiedIntegration }) => (
    <Card className="h-full hover:shadow-2xl hover:scale-[1.02] hover:-translate-y-1 transition-all duration-300">
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            {integration.icon}
            <CardTitle className="text-lg">{integration.name}</CardTitle>
          </div>
          {integration.status === 'connected' ? (
            <Badge className="bg-gradient-to-r from-green-500 to-emerald-500 text-white border-0">
              <CheckCircle2 className="h-3 w-3 mr-1" />
              Connected
            </Badge>
          ) : integration.setup_type === 'coming_soon' ? (
            <Badge className="bg-white/50 dark:bg-white/10 backdrop-blur-md border-white/20">
              <Star className="h-3 w-3 mr-1" />
              Coming Soon
            </Badge>
          ) : (
            <Badge className="bg-amber-500/10 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 backdrop-blur-md border-amber-500/20">
              <XCircle className="h-3 w-3 mr-1" />
              Available
            </Badge>
          )}
        </div>
        <CardDescription>{integration.description}</CardDescription>
      </CardHeader>

      <CardContent>
        <div className="space-y-3">
          <div className="flex gap-2">
            {getActionButton(integration)}
            {integration.id === 'google' && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setGoogleDialog(true)}
                className="flex items-center gap-1"
              >
                <ArrowRight className="h-3 w-3" />
                Details
              </Button>
            )}
          </div>

          {/* Service-specific help text */}
          {integration.id === 'imdb' && integration.status !== 'connected' && (
            <div className="text-xs text-gray-500 dark:text-gray-400">
              Get a free API key from{' '}
              <a
                href="http://www.omdbapi.com/apikey.aspx"
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-500 dark:text-blue-400 underline"
              >
                OMDb API
              </a>
            </div>
          )}
          {integration.id === 'spotify' && integration.status !== 'connected' && (
            <div className="text-xs text-gray-500 dark:text-gray-400">
              Requires Spotify app credentials from the Developer Dashboard
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );

  // Filter integrations based on availability
  const filteredIntegrations = showOnlyAvailable
    ? integrations.filter(i => i.status === 'available' || i.status === 'connected')
    : integrations;

  const allCategories = [
    { id: 'all', name: 'All', count: filteredIntegrations.length },
    {
      id: 'email',
      name: 'Email & Calendar',
      count: filteredIntegrations.filter(i => i.category === 'email').length,
    },
    {
      id: 'shopping',
      name: 'Shopping',
      count: filteredIntegrations.filter(i => i.category === 'shopping').length,
    },
    {
      id: 'entertainment',
      name: 'Entertainment',
      count: filteredIntegrations.filter(i => i.category === 'entertainment').length,
    },
    {
      id: 'social',
      name: 'Social',
      count: filteredIntegrations.filter(i => i.category === 'social').length,
    },
    {
      id: 'development',
      name: 'Development',
      count: filteredIntegrations.filter(i => i.category === 'development').length,
    },
    {
      id: 'information',
      name: 'Information',
      count: filteredIntegrations.filter(i => i.category === 'information').length,
    },
  ];

  // Filter out empty categories when showing only available
  const categories = showOnlyAvailable ? allCategories.filter(cat => cat.count > 0) : allCategories;

  const getIntegrationsForCategory = (categoryId: string) => {
    if (categoryId === 'all') return filteredIntegrations;
    return filteredIntegrations.filter(i => i.category === categoryId);
  };

  // Show loading state while fetching tools
  if (toolsLoading && integrations.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4" />
          <p className="text-gray-600 dark:text-gray-400">Loading available tools...</p>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="h-full flex flex-col">
        <div className="flex-shrink-0 mb-4">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-2xl font-bold">Service Integrations</h2>
            <ToggleGroup
              type="single"
              value={showOnlyAvailable ? 'available' : 'all'}
              onValueChange={(value: string) => setShowOnlyAvailable(value === 'available')}
              className="backdrop-blur-md bg-white/50 dark:bg-white/10 border border-white/20 dark:border-white/10 rounded-lg"
            >
              <ToggleGroupItem value="all" aria-label="Show all services">
                <span className="text-xs font-medium">Show All</span>
              </ToggleGroupItem>
              <ToggleGroupItem value="available" aria-label="Show available only">
                <span className="text-xs font-medium">Available Only</span>
              </ToggleGroupItem>
            </ToggleGroup>
          </div>
          <p className="text-gray-600 dark:text-gray-400">
            Connect external services to enable agent capabilities
          </p>
          <div className="mt-2 p-3 backdrop-blur-md bg-gradient-to-r from-amber-500/10 to-orange-500/10 dark:from-amber-500/20 dark:to-orange-500/20 rounded-lg border border-white/20 dark:border-white/10">
            <p className="text-sm text-amber-700 dark:text-amber-300">
              {showOnlyAvailable ? (
                <>
                  <strong>Available Services:</strong> Gmail (via Google), IMDb, Spotify
                </>
              ) : (
                <>
                  <strong>Currently Available:</strong> Gmail (via Google), IMDb, Spotify •{' '}
                  <strong>Coming Soon:</strong> 20+ services across shopping, social, entertainment
                  & more
                </>
              )}
            </p>
          </div>
        </div>

        <div className="flex-1 overflow-auto">
          <Tabs defaultValue="all" className="h-full">
            <TabsList className="flex flex-wrap">
              {categories.map(category => (
                <TabsTrigger
                  key={category.id}
                  value={category.id}
                  className="text-xs flex-shrink-0"
                >
                  {category.name}
                  {category.count > 0 && (
                    <Badge variant="secondary" className="ml-1 text-xs">
                      {category.count}
                    </Badge>
                  )}
                </TabsTrigger>
              ))}
            </TabsList>

            {categories.map(category => (
              <TabsContent key={category.id} value={category.id} className="mt-4">
                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                  {getIntegrationsForCategory(category.id).map(integration => (
                    <IntegrationCard key={integration.id} integration={integration} />
                  ))}
                </div>
              </TabsContent>
            ))}
          </Tabs>
        </div>
      </div>

      {/* Google Integration Dialog */}
      <Dialog open={googleDialog} onOpenChange={setGoogleDialog}>
        <DialogContent className="max-w-4xl max-h-[90vh] overflow-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <img src="https://www.google.com/favicon.ico" alt="Google" className="w-5 h-5" />
              Google Services Setup
            </DialogTitle>
            <DialogDescription>Configure Gmail, Calendar, and Drive integration</DialogDescription>
          </DialogHeader>
          <GoogleIntegration />
        </DialogContent>
      </Dialog>

      {/* IMDb Configuration Dialog */}
      <Dialog
        open={configDialog.open && configDialog.service === 'imdb'}
        onOpenChange={open => setConfigDialog({ open })}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Configure IMDb (OMDb API)</DialogTitle>
            <DialogDescription>
              Enter your OMDb API key to enable movie and TV show searches
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div>
              <Label htmlFor="api-key">API Key</Label>
              <Input
                id="api-key"
                type="password"
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
                placeholder="Enter your OMDb API key"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
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
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setConfigDialog({ open: false })}>
              Cancel
            </Button>
            <Button onClick={configureImdb} disabled={!apiKey || loading}>
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Configure'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
