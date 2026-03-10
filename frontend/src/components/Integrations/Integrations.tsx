import { useState, useEffect, useMemo } from 'react';
import {
  ArrowRight,
  Settings,
  CheckCircle2,
  Circle,
  Loader2,
  Key,
  ExternalLink,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useToast } from '@/components/ui/use-toast';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useTools, mapToolToIntegration } from '@/hooks/useTools';
import { useConfigStore } from '@/store/configStore';
import { getIconForTool } from './iconMapping';
import { API_BASE_URL, withAgentName } from '@/lib/api';
import {
  Integration,
  IntegrationConfig,
  integrationProviders,
  getAllIntegrations,
} from './integrations/index';
import { EnhancedConfigDialog } from './EnhancedConfigDialog';
import { FilterSelector } from '@/components/shared/FilterSelector';

const SHARED_ONLY_PROVIDER_IDS = new Set(['google', 'spotify', 'homeassistant']);
const SHARED_ONLY_BACKEND_TOOL_IDS = new Set([
  'spotify',
  'homeassistant',
  'gmail',
  'google_calendar',
  'google_sheets',
]);

export function Integrations() {
  const { agents } = useConfigStore();
  const [scopeAgentName, setScopeAgentName] = useState<string | null>(null);
  const scopedAgents = useMemo(
    () =>
      agents
        .filter(agent => agent.worker_scope !== 'room_thread')
        .sort((a, b) => a.display_name.localeCompare(b.display_name)),
    [agents]
  );
  const roomThreadAgents = useMemo(
    () => agents.filter(agent => agent.worker_scope === 'room_thread'),
    [agents]
  );
  const selectedScopeAgent = useMemo(
    () => scopedAgents.find(agent => agent.id === scopeAgentName) ?? null,
    [scopedAgents, scopeAgentName]
  );
  const selectedWorkerScope = selectedScopeAgent?.worker_scope ?? null;
  const hidesSharedOnlyIntegrations =
    selectedScopeAgent !== null && selectedWorkerScope !== null && selectedWorkerScope !== 'shared';
  const disablesDashboardCredentialManagement = hidesSharedOnlyIntegrations;

  // Fetch tools from backend
  const {
    tools: backendTools,
    loading: toolsLoading,
    refetch: refetchTools,
  } = useTools(scopeAgentName);

  // State
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeDialog, setActiveDialog] = useState<{
    integrationId: string;
    config: IntegrationConfig;
  } | null>(null);
  const [configDialog, setConfigDialog] = useState<{
    service: string;
    displayName: string;
    description: string;
    configFields: any[];
    isEditing?: boolean;
    docsUrl?: string | null;
    helperText?: string | null;
    icon?: any;
    iconColor?: string;
  } | null>(null);
  const [filterMode, setFilterMode] = useState<'all' | 'available' | 'unconfigured' | 'configured'>(
    'all'
  );
  const [searchTerm, setSearchTerm] = useState('');
  const { toast } = useToast();

  useEffect(() => {
    if (scopeAgentName && !scopedAgents.some(agent => agent.id === scopeAgentName)) {
      setScopeAgentName(null);
    }
  }, [scopeAgentName, scopedAgents]);

  // Load integrations from providers and backend tools
  useEffect(() => {
    loadIntegrations();
  }, [backendTools, hidesSharedOnlyIntegrations, scopeAgentName]);

  const loadIntegrations = async (forceRefresh = false) => {
    setLoading(true);
    try {
      // Optionally refetch tools from backend to get updated statuses
      // This is important after Google OAuth to get the new status for Google tools
      if (forceRefresh) {
        await refetchTools();
        // Return early since refetchTools will trigger this useEffect again via backendTools update
        setLoading(false);
        return;
      }

      const loadedIntegrations: Integration[] = [];
      const scope = { agentName: scopeAgentName };

      // Load special integrations from providers
      for (const provider of getAllIntegrations()) {
        const config = provider.getConfig(scope);
        if (hidesSharedOnlyIntegrations && SHARED_ONLY_PROVIDER_IDS.has(config.integration.id)) {
          continue;
        }
        const status = provider.loadStatus ? await provider.loadStatus(scope) : {};
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
        .filter(
          tool => !hidesSharedOnlyIntegrations || !SHARED_ONLY_BACKEND_TOOL_IDS.has(tool.name)
        )
        .map(tool => {
          const mapped = mapToolToIntegration(tool);
          return {
            ...mapped,
            icon: getIconForTool(tool.icon, tool.icon_color),
            connected: false,
            // Tools with auth_provider show as connected if their status is 'available'
            status: tool.auth_provider && tool.status === 'available' ? 'connected' : mapped.status,
            auth_provider: tool.auth_provider, // Pass through auth_provider
          } as Integration & { auth_provider?: string };
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

  const integrationNeedsDashboardCredentials = (
    integration: Integration & { auth_provider?: string }
  ) => {
    const tool = integration as any;
    return (
      integration.setup_type !== 'none' ||
      Boolean(tool.auth_provider) ||
      Boolean(tool.config_fields && tool.config_fields.length > 0)
    );
  };

  const handleIntegrationAction = async (integration: Integration) => {
    if (
      disablesDashboardCredentialManagement &&
      integrationNeedsDashboardCredentials(integration)
    ) {
      toast({
        title: 'Shared-only dashboard configuration',
        description:
          'Dashboard credential setup is only supported for shared deployment credentials.',
        variant: 'destructive',
      });
      return;
    }

    // Check if we have a provider for this integration
    const provider = integrationProviders[integration.id];
    const scope = { agentName: scopeAgentName };

    if (provider) {
      const config = provider.getConfig(scope);

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
    } else if (
      integration.setup_type === 'api_key' ||
      integration.setup_type === 'oauth' ||
      integration.setup_type === 'special' ||
      integration.setup_type === 'none'
    ) {
      // Show generic config dialog for tools with config_fields
      const tool = integration as any; // Cast to access config_fields
      if (tool.config_fields && tool.config_fields.length > 0) {
        setConfigDialog({
          service: integration.id,
          displayName: integration.name,
          description: integration.description,
          configFields: tool.config_fields,
          isEditing: integration.status === 'connected',
          docsUrl: tool.docs_url || null,
          helperText: tool.helper_text || null,
          icon: null, // Icon loaded from integration object or backend
          iconColor: tool.icon_color || integration.iconColor,
        });
      } else {
        toast({
          title: 'Configuration Error',
          description: `${integration.name} requires configuration but no fields are specified.`,
          variant: 'destructive',
        });
      }
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
    if (
      disablesDashboardCredentialManagement &&
      integrationNeedsDashboardCredentials(integration)
    ) {
      toast({
        title: 'Shared-only dashboard configuration',
        description:
          'Dashboard credential editing is only supported for shared deployment credentials.',
        variant: 'destructive',
      });
      return;
    }

    const provider = integrationProviders[integration.id];
    const scope = { agentName: scopeAgentName };

    setLoading(true);
    try {
      if (provider?.getConfig(scope).onDisconnect) {
        // Use provider's disconnect method if available
        await provider.getConfig(scope).onDisconnect!(integration.id);
      } else {
        // For generic tools, delete credentials via API
        const response = await fetch(
          withAgentName(`${API_BASE_URL}/api/credentials/${integration.id}`, scopeAgentName),
          {
            method: 'DELETE',
          }
        );

        if (!response.ok) {
          throw new Error('Failed to disconnect');
        }
      }

      // Refetch tools to update status
      await refetchTools();

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
  };

  const getActionButton = (integration: Integration) => {
    if (
      disablesDashboardCredentialManagement &&
      integrationNeedsDashboardCredentials(integration)
    ) {
      return (
        <Button disabled variant="outline" size="sm">
          Shared-only config
        </Button>
      );
    }

    // Check if there's a custom action button
    const provider = integrationProviders[integration.id];
    const config = provider?.getConfig({ agentName: scopeAgentName });

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

    // Handle tools with delegated authentication
    const tool = integration as any;
    if (tool.auth_provider) {
      // Check if the auth provider is connected
      const authProvider = integrations.find(i => i.id === tool.auth_provider);

      if (integration.status === 'connected' || integration.status === 'available') {
        // Auth provider is connected
        if (tool.config_fields && tool.config_fields.length > 0) {
          return (
            <div className="flex gap-2 items-center">
              <Badge className="bg-green-500/10 dark:bg-green-500/20 text-green-700 dark:text-green-300">
                <CheckCircle2 className="h-3 w-3 mr-1" />
                Connected
              </Badge>
              <Button
                onClick={() => handleIntegrationAction(integration)}
                disabled={loading}
                variant="outline"
                size="sm"
              >
                <Settings className="h-4 w-4 mr-1" />
                Configure
              </Button>
            </div>
          );
        } else {
          // Tool with no additional config
          return (
            <Badge className="bg-green-500/10 dark:bg-green-500/20 text-green-700 dark:text-green-300">
              <CheckCircle2 className="h-3 w-3 mr-1" />
              Connected
            </Badge>
          );
        }
      } else {
        // Auth provider not connected
        return (
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-muted-foreground">
              Requires {authProvider?.name || tool.auth_provider}
            </Badge>
            <Button
              onClick={() => {
                if (authProvider) {
                  handleIntegrationAction(authProvider);
                } else {
                  toast({
                    title: `Connect ${tool.auth_provider} first`,
                    description: `Please connect to ${tool.auth_provider} to use this tool.`,
                  });
                }
              }}
              disabled={loading}
              variant="outline"
              size="sm"
            >
              <ExternalLink className="h-4 w-4 mr-1" />
              Setup
            </Button>
          </div>
        );
      }
    }

    // Tools with no setup required
    if (integration.setup_type === 'none') {
      const tool = integration as any;
      // Check if there are optional config fields
      if (tool.config_fields && tool.config_fields.length > 0) {
        // Check if any configuration has been saved
        const hasConfig = integration.status === 'connected';

        if (hasConfig) {
          // Show edit/reset buttons for configured tools
          return (
            <div className="flex gap-2">
              <Button
                onClick={() => handleIntegrationAction(integration)}
                disabled={loading}
                variant="outline"
                size="sm"
              >
                <Settings className="h-4 w-4 mr-1" />
                Settings
              </Button>
              <Button
                onClick={() => handleDisconnect(integration)}
                disabled={loading}
                variant="ghost"
                size="sm"
              >
                Reset
              </Button>
            </div>
          );
        } else {
          // Show optional configure button
          return (
            <div className="flex gap-2 items-center">
              <Badge className="bg-green-500/10 dark:bg-green-500/20 text-green-700 dark:text-green-300">
                <CheckCircle2 className="h-3 w-3 mr-1" />
                Ready
              </Badge>
              <Button
                onClick={() => handleIntegrationAction(integration)}
                disabled={loading}
                variant="outline"
                size="sm"
              >
                <Settings className="h-4 w-4 mr-1" />
                Configure
              </Button>
            </div>
          );
        }
      } else {
        // No config fields, just show ready status
        return (
          <Badge className="bg-green-500/10 dark:bg-green-500/20 text-green-700 dark:text-green-300">
            <CheckCircle2 className="h-3 w-3 mr-1" />
            Ready to Use
          </Badge>
        );
      }
    }

    // For other connected tools, show Edit/Disconnect
    if (integration.status === 'connected') {
      return (
        <div className="flex gap-2">
          <Button
            onClick={() => handleIntegrationAction(integration)}
            disabled={loading}
            variant="outline"
            size="sm"
          >
            Edit
          </Button>
          <Button
            onClick={() => handleDisconnect(integration)}
            disabled={loading}
            variant="destructive"
            size="sm"
          >
            Disconnect
          </Button>
        </div>
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

  const IntegrationCard = ({
    integration,
  }: {
    integration: Integration & { auth_provider?: string };
  }) => (
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
          ) : (
            <Badge className="bg-amber-500/10 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 backdrop-blur-md border-amber-500/20">
              <Circle className="h-3 w-3 mr-1" />
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
        </div>
      </CardContent>
    </Card>
  );

  // Filter integrations
  const filteredIntegrations = useMemo(() => {
    let filtered = integrations;

    // Filter by mode
    switch (filterMode) {
      case 'available':
        filtered = filtered.filter(i => i.status === 'available' || i.status === 'connected');
        break;
      case 'unconfigured':
        filtered = filtered.filter(i => i.status !== 'connected' && i.setup_type !== 'none');
        break;
      case 'configured':
        filtered = filtered.filter(i => i.status === 'connected');
        break;
      // 'all' - no filtering needed
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
  }, [integrations, filterMode, searchTerm]);

  const categories = useMemo(() => {
    const allCategories = [
      { id: 'all', name: 'All', count: filteredIntegrations.length },
      {
        id: 'email',
        name: 'Email & Calendar',
        count: filteredIntegrations.filter(i => i.category === 'email').length,
      },
      {
        id: 'communication',
        name: 'Communication',
        count: filteredIntegrations.filter(i => i.category === 'communication').length,
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
        id: 'research',
        name: 'Research',
        count: filteredIntegrations.filter(i => i.category === 'research').length,
      },
      {
        id: 'smart_home',
        name: 'Smart Home',
        count: filteredIntegrations.filter(i => i.category === 'smart_home').length,
      },
      {
        id: 'information',
        name: 'Information',
        count: filteredIntegrations.filter(i => i.category === 'information').length,
      },
    ];

    return filterMode !== 'all' ? allCategories.filter(cat => cat.count > 0) : allCategories;
  }, [filteredIntegrations, filterMode]);

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
            <h2 className="text-2xl font-bold">Tools</h2>
            <div className="flex items-center gap-2">
              <Select
                value={scopeAgentName ?? 'shared'}
                onValueChange={value => setScopeAgentName(value === 'shared' ? null : value)}
              >
                <SelectTrigger className="w-72">
                  <SelectValue placeholder="Shared deployment credentials" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="shared">Shared deployment credentials</SelectItem>
                  {scopedAgents.map(agent => (
                    <SelectItem key={agent.id} value={agent.id}>
                      {agent.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Input
                type="search"
                placeholder="Search tools..."
                value={searchTerm}
                onChange={e => setSearchTerm(e.target.value)}
                className="w-64"
              />
              <FilterSelector
                options={[
                  { value: 'all', label: 'Show All' },
                  { value: 'available', label: 'Available' },
                  { value: 'unconfigured', label: 'Unconfigured' },
                  { value: 'configured', label: 'Configured' },
                ]}
                value={filterMode}
                onChange={value =>
                  setFilterMode(value as 'all' | 'available' | 'unconfigured' | 'configured')
                }
                size="sm"
              />
            </div>
          </div>
          <p className="text-gray-600 dark:text-gray-400">
            {selectedScopeAgent
              ? `Configuring tools for ${selectedScopeAgent.display_name} (${
                  selectedWorkerScope ?? 'shared'
                } worker scope).`
              : 'Connect external services to enable agent capabilities'}
          </p>
          {roomThreadAgents.length > 0 && (
            <Alert className="mt-3">
              <AlertDescription>
                Room-thread scoped agents are not configurable here because their worker is selected
                at runtime from a room and thread.
              </AlertDescription>
            </Alert>
          )}
          {hidesSharedOnlyIntegrations && (
            <Alert className="mt-3">
              <AlertDescription>
                Dashboard credential setup, editing, and disconnect are only supported for shared
                deployment credentials.
              </AlertDescription>
            </Alert>
          )}
          {hidesSharedOnlyIntegrations && (
            <Alert className="mt-3">
              <AlertDescription>
                Google Services, Home Assistant, Spotify, Gmail, Google Calendar, and Google Sheets
                are only supported for shared deployment credentials or agents with
                `worker_scope=shared`.
              </AlertDescription>
            </Alert>
          )}
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
                agentName={scopeAgentName}
                onSuccess={async () => {
                  setActiveDialog(null);
                  // Force refresh to get updated Google tools status
                  await loadIntegrations(true);
                }}
              />
            )}
          </DialogContent>
        </Dialog>
      )}

      {/* Enhanced Configuration Dialog */}
      {configDialog && (
        <EnhancedConfigDialog
          open={true}
          onClose={() => setConfigDialog(null)}
          service={configDialog.service}
          displayName={configDialog.displayName}
          description={configDialog.description}
          configFields={configDialog.configFields}
          isEditing={configDialog.isEditing}
          docsUrl={configDialog.docsUrl}
          helperText={configDialog.helperText}
          icon={configDialog.icon}
          iconColor={configDialog.iconColor}
          agentName={scopeAgentName}
          onSuccess={async () => {
            setConfigDialog(null);
            // Refetch tools to get updated status
            await refetchTools();
          }}
        />
      )}
    </>
  );
}
