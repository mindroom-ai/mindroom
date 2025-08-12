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
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useToast } from '@/components/ui/use-toast';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useTools, mapToolToIntegration } from '@/hooks/useTools';
import { getIconForTool } from './iconMapping';
import {
  Integration,
  IntegrationConfig,
  integrationProviders,
  getAllIntegrations,
} from './integrations';

export function Integrations() {
  // Fetch tools from backend
  const { tools: backendTools, loading: toolsLoading } = useTools();

  // State
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeDialog, setActiveDialog] = useState<{
    integrationId: string;
    config: IntegrationConfig;
  } | null>(null);
  const [showOnlyAvailable, setShowOnlyAvailable] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const { toast } = useToast();

  // Load integrations from providers and backend tools
  useEffect(() => {
    loadIntegrations();
  }, [backendTools]);

  const loadIntegrations = async () => {
    setLoading(true);
    try {
      const loadedIntegrations: Integration[] = [];

      // Load special integrations from providers
      for (const provider of getAllIntegrations()) {
        const config = provider.getConfig();
        const status = provider.loadStatus ? await provider.loadStatus() : {};
        loadedIntegrations.push({
          ...config.integration,
          ...status,
        });
      }

      // Load backend tools and map them to integrations
      // (excluding those already handled by providers)
      const providerIds = Object.keys(integrationProviders);
      const backendIntegrations = backendTools
        .filter(tool => !providerIds.includes(tool.name))
        .map(tool => {
          const mapped = mapToolToIntegration(tool);
          return {
            ...mapped,
            icon: getIconForTool(tool.icon),
            connected: false,
          } as Integration;
        });

      setIntegrations([...loadedIntegrations, ...backendIntegrations]);
    } catch (error) {
      console.error('Failed to load integrations:', error);
      toast({
        title: 'Error',
        description: 'Failed to load integrations',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleIntegrationAction = async (integration: Integration) => {
    // Check if we have a provider for this integration
    const provider = integrationProviders[integration.id];

    if (provider) {
      const config = provider.getConfig();

      // If there's a custom config component, show it in a dialog
      if (config.ConfigComponent) {
        setActiveDialog({ integrationId: integration.id, config });
        return;
      }

      // Otherwise, execute the action directly
      if (config.onAction) {
        setLoading(true);
        try {
          await config.onAction(integration);
          await loadIntegrations(); // Reload status
        } catch (error) {
          toast({
            title: 'Action Failed',
            description: error instanceof Error ? error.message : 'Failed to perform action',
            variant: 'destructive',
          });
        } finally {
          setLoading(false);
        }
      }
    } else if (integration.setup_type === 'coming_soon') {
      toast({
        title: 'Coming Soon',
        description: `${integration.name} integration is in development and will be available soon.`,
      });
    } else {
      // Fallback for integrations without providers yet
      toast({
        title: 'Not Implemented',
        description: `${integration.name} integration is not yet implemented.`,
        variant: 'destructive',
      });
    }
  };

  const handleDisconnect = async (integration: Integration) => {
    const provider = integrationProviders[integration.id];

    if (provider?.getConfig().onDisconnect) {
      setLoading(true);
      try {
        await provider.getConfig().onDisconnect!(integration.id);
        await loadIntegrations(); // Reload status
        toast({
          title: 'Disconnected',
          description: `${integration.name} has been disconnected.`,
        });
      } catch (error) {
        toast({
          title: 'Disconnect Failed',
          description: error instanceof Error ? error.message : 'Failed to disconnect',
          variant: 'destructive',
        });
      } finally {
        setLoading(false);
      }
    }
  };

  const getActionButton = (integration: Integration) => {
    // Check if there's a custom action button
    const provider = integrationProviders[integration.id];
    const config = provider?.getConfig();

    if (config?.ActionButton) {
      const ActionButton = config.ActionButton;
      return (
        <ActionButton
          integration={integration}
          loading={loading}
          onAction={() => handleIntegrationAction(integration)}
        />
      );
    }

    // Default button rendering
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
          onClick={() => handleDisconnect(integration)}
          disabled={loading}
          variant="destructive"
          size="sm"
        >
          Disconnect
        </Button>
      );
    }

    const buttonText =
      integration.setup_type === 'special'
        ? 'Setup'
        : integration.setup_type === 'oauth'
          ? 'Connect'
          : 'Configure';

    const icon =
      integration.setup_type === 'special' ? (
        <Settings className="h-4 w-4" />
      ) : integration.setup_type === 'oauth' ? (
        <ExternalLink className="h-4 w-4" />
      ) : (
        <Key className="h-4 w-4" />
      );

    return (
      <Button
        onClick={() => handleIntegrationAction(integration)}
        disabled={loading}
        size="sm"
        className="flex items-center gap-2"
      >
        {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
        {buttonText}
      </Button>
    );
  };

  const IntegrationCard = ({ integration }: { integration: Integration }) => (
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
                onClick={() => handleIntegrationAction(integration)}
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

  // Filter integrations
  const filteredIntegrations = useMemo(() => {
    let filtered = integrations;

    // Filter by availability
    if (showOnlyAvailable) {
      filtered = filtered.filter(i => i.status === 'available' || i.status === 'connected');
    }

    // Filter by search term
    if (searchTerm) {
      filtered = filtered.filter(
        i =>
          i.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
          i.description.toLowerCase().includes(searchTerm.toLowerCase())
      );
    }

    return filtered;
  }, [integrations, showOnlyAvailable, searchTerm]);

  const categories = useMemo(() => {
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

    return showOnlyAvailable ? allCategories.filter(cat => cat.count > 0) : allCategories;
  }, [filteredIntegrations, showOnlyAvailable]);

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
            <div className="flex items-center gap-2">
              <Input
                type="search"
                placeholder="Search integrations..."
                value={searchTerm}
                onChange={e => setSearchTerm(e.target.value)}
                className="w-64"
              />
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
          </div>
          <p className="text-gray-600 dark:text-gray-400">
            Connect external services to enable agent capabilities
          </p>
          <div className="mt-2 p-3 backdrop-blur-md bg-gradient-to-r from-amber-500/10 to-orange-500/10 dark:from-amber-500/20 dark:to-orange-500/20 rounded-lg border border-white/20 dark:border-white/10">
            <p className="text-sm text-amber-700 dark:text-amber-300">
              <strong>Currently Available:</strong> Gmail (via Google), IMDb, Spotify •{' '}
              <strong>Coming Soon:</strong> 20+ services across shopping, social, entertainment &
              more
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

      {/* Dynamic Configuration Dialog */}
      {activeDialog && (
        <Dialog open={true} onOpenChange={open => !open && setActiveDialog(null)}>
          <DialogContent className="max-w-4xl max-h-[90vh] overflow-auto">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                {activeDialog.config.integration.icon}
                {activeDialog.config.integration.name} Setup
              </DialogTitle>
              <DialogDescription>{activeDialog.config.integration.description}</DialogDescription>
            </DialogHeader>
            {activeDialog.config.ConfigComponent && (
              <activeDialog.config.ConfigComponent
                onClose={() => setActiveDialog(null)}
                onSuccess={async () => {
                  setActiveDialog(null);
                  await loadIntegrations();
                }}
              />
            )}
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}
