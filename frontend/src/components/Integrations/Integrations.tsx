import {
  useState,
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import {
  Settings,
  CheckCircle2,
  Circle,
  Loader2,
  Key,
  ExternalLink,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/components/ui/use-toast";
import { apiErrorMessageFromResponse } from "@/lib/api";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  useTools,
  mapToolToIntegration,
  type ToolInfo,
  type ToolFieldSchema,
} from "@/hooks/useTools";
import { useConfigStore } from "@/store/configStore";
import { getIconForTool } from "./iconMapping";
import {
  builtInProviderCards,
  oauthCard,
  oauthIntegrationProvider,
  scopedApiUrl,
} from "./integrations/index";
import type {
  Integration,
  IntegrationProvider,
  IntegrationStatus,
  ProviderCard,
} from "./integrations/types";
import { homeAssistantIntegration } from "./integrations/homeassistant";
import { EnhancedConfigDialog } from "./EnhancedConfigDialog";
import { FilterSelector } from "@/components/shared/FilterSelector";

const SHARED_ONLY_PROVIDER_IDS = new Set(["spotify", "homeassistant"]);
const NO_INTEGRATIONS: Integration[] = [];

type FilterMode = "all" | "available" | "unconfigured" | "configured";

interface DisconnectToast {
  title: string;
  description: string;
}

const CATEGORIES = [
  { id: "email", name: "Email & Calendar" },
  { id: "productivity", name: "Productivity" },
  { id: "communication", name: "Communication" },
  { id: "entertainment", name: "Entertainment" },
  { id: "social", name: "Social" },
  { id: "development", name: "Development" },
  { id: "research", name: "Research" },
  { id: "smart_home", name: "Smart Home" },
  { id: "integrations", name: "Integrations" },
  { id: "information", name: "Information" },
];

const SuccessBadge = ({
  icon: Icon,
  children,
}: {
  icon: LucideIcon;
  children: ReactNode;
}) => (
  <Badge className="bg-green-500/10 dark:bg-green-500/20 text-green-700 dark:text-green-300">
    <Icon className="h-3 w-3 mr-1" />
    {children}
  </Badge>
);

const titleFromProviderId = (providerId: string) =>
  providerId
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");

// Several tools can share one provider connection, so no single tool's settings can stand for its card.
const providersSharedByTools = (tools: ToolInfo[]) => {
  const toolNames = new Set(tools.map((tool) => tool.name));
  const toolCounts = new Map<string, number>();
  for (const tool of tools) {
    if (tool.auth_provider) {
      toolCounts.set(
        tool.auth_provider,
        (toolCounts.get(tool.auth_provider) ?? 0) + 1,
      );
    }
  }
  return new Set(
    [...toolCounts]
      .filter(([providerId, count]) => count > 1 && !toolNames.has(providerId))
      .map(([providerId]) => providerId),
  );
};

const builtInCardsById = new Map(
  builtInProviderCards.map((card) => [card.id, card]),
);

const providerForIntegration = (
  integration: ProviderCard,
): IntegrationProvider | null => {
  if (integration.id === "homeassistant") {
    return homeAssistantIntegration;
  }
  if (!integration.oauth_provider_id) {
    return null;
  }
  return oauthIntegrationProvider(
    integration.oauth_provider_id,
    integration.name,
  );
};

// A provider known only from tool metadata takes its card's text and icon from the tool that stands for it.
const discoveredOAuthCard = (
  providerId: string,
  providerTool?: ToolInfo,
): ProviderCard => {
  const name =
    providerTool?.display_name.replace(/\s+Tool$/, "") ??
    titleFromProviderId(providerId);
  return oauthCard(
    providerId,
    name,
    `Connect ${name} OAuth for ${providerTool?.display_name ?? "this tool"}.`,
    providerTool?.category ?? "productivity",
    getIconForTool(providerTool?.icon ?? null, providerTool?.icon_color),
  );
};

const hasConfigFields = (integration: Integration) =>
  Boolean(integration.config_fields?.length);

const hasManualOAuthFallback = (integration: Integration) =>
  integration.setup_type === "oauth" &&
  Boolean(integration.oauth_fallback_fields?.length);

const hasConfiguredOAuthFallback = (integration?: {
  manual_auth_configured?: boolean;
  environment_auth_configured?: boolean;
}) =>
  integration?.manual_auth_configured === true ||
  integration?.environment_auth_configured === true;

const withProviderState = (
  card: ProviderCard,
  providerTool: ToolInfo | undefined,
  status: IntegrationStatus,
): Integration => {
  const fallbackConfigured = hasConfiguredOAuthFallback(providerTool);
  return {
    ...card,
    config_fields: providerTool?.config_fields,
    docs_url: providerTool?.docs_url,
    oauth_fallback_fields: providerTool?.oauth_fallback_fields,
    manual_auth_configured: providerTool?.manual_auth_configured,
    environment_auth_configured: providerTool?.environment_auth_configured,
    ...status,
    status: fallbackConfigured ? "connected" : status.status,
    status_error: fallbackConfigured ? undefined : status.status_error,
    helper_text:
      fallbackConfigured && status.oauth_reset_required !== true
        ? providerTool?.helper_text
        : (status.helper_text ?? providerTool?.helper_text),
    config_service: providerTool?.name,
  };
};

const manualOAuthFallbackLabel = (integration: Integration) => {
  const fallbackFields = integration.oauth_fallback_fields ?? [];
  if (fallbackFields.length !== 1) {
    return "Manual credentials";
  }
  return (
    integration.config_fields?.find((field) => field.name === fallbackFields[0])
      ?.label ?? "Manual credentials"
  );
};

const IntegrationCard = ({
  integration,
  action,
}: {
  integration: Integration;
  action: ReactNode;
}) => {
  const isConnected = integration.status === "connected";
  const usesGoogleUserData = integration.id.startsWith("google_");
  const statusLabel = integration.status_error
    ? "Status error"
    : integration.status === "not_connected"
      ? "Needs setup"
      : "Available";

  return (
    <Card className="h-full">
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            {integration.icon}
            <CardTitle className="text-lg">{integration.name}</CardTitle>
          </div>
          {isConnected ? (
            <SuccessBadge icon={CheckCircle2}>Connected</SuccessBadge>
          ) : (
            <Badge className="bg-amber-500/10 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 border-amber-500/20">
              <Circle className="h-3 w-3 mr-1" />
              {statusLabel}
            </Badge>
          )}
        </div>
        <CardDescription>{integration.description}</CardDescription>
        {usesGoogleUserData ? (
          <p className="text-xs text-muted-foreground">
            By connecting, you authorize this MindRoom installation to access
            Google data for the selected agent and credential scope. Shared or
            unscoped agents can use the connected account for any user
            authorized to invoke them. Relevant results may be sent to your
            configured AI model and retained in session or Matrix history. The
            installation operator and anyone with administrative or filesystem
            access may be able to access stored credentials and data. Project
            maintainers have no automatic access merely because they maintain
            MindRoom; if they also operate this installation, they may have
            operator access.{" "}
            <a
              href="https://docs.mindroom.chat/privacy/"
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2"
            >
              Privacy policy
            </a>
            .
          </p>
        ) : null}
        {integration.status_error ? (
          <p className="text-xs text-muted-foreground">
            Status error: {integration.status_error}
          </p>
        ) : integration.helper_text ? (
          <p className="text-xs text-muted-foreground">
            {integration.helper_text}
          </p>
        ) : null}
      </CardHeader>

      <CardContent>
        <div className="space-y-3">
          <div className="flex gap-2">{action}</div>
        </div>
      </CardContent>
    </Card>
  );
};

export function Integrations() {
  const { agents, agentPoliciesByAgent } = useConfigStore();
  const [scopeAgentName, setScopeAgentName] = useState<string | null>(null);
  const scopedAgents = useMemo(
    () =>
      agents
        .filter(
          (agent) =>
            agentPoliciesByAgent[agent.id]?.effective_execution_scope != null,
        )
        .sort((a, b) => a.display_name.localeCompare(b.display_name)),
    [agentPoliciesByAgent, agents],
  );
  const selectedScopeAgent = useMemo(
    () => scopedAgents.find((agent) => agent.id === scopeAgentName) ?? null,
    [scopedAgents, scopeAgentName],
  );
  const effectiveScopeAgentName = selectedScopeAgent?.id ?? null;
  const selectedScopePolicy =
    selectedScopeAgent != null
      ? (agentPoliciesByAgent[selectedScopeAgent.id] ?? null)
      : null;
  const selectedExecutionScope =
    selectedScopePolicy?.effective_execution_scope ?? null;
  const selectedScopeLabel = selectedScopePolicy?.scope_label ?? null;
  const hidesSharedOnlyIntegrations =
    selectedExecutionScope !== null && selectedExecutionScope !== "shared";
  const disablesDashboardCredentialManagement =
    selectedScopePolicy?.dashboard_credentials_supported === false;

  const {
    tools: backendTools,
    loading: toolsLoading,
    refetch: refetchTools,
    statusAuthoritative,
  } = useTools(effectiveScopeAgentName, selectedExecutionScope);
  const currentScopeKey = `${effectiveScopeAgentName ?? "shared"}:${
    selectedExecutionScope ?? "unscoped"
  }`;

  const [integrationsState, setIntegrationsState] = useState<{
    scopeKey: string;
    integrations: Integration[];
  }>({
    scopeKey: "",
    integrations: [],
  });
  const [loading, setLoading] = useState(false);
  const loadRequestIdRef = useRef(0);
  const currentScopeKeyRef = useRef(currentScopeKey);
  useLayoutEffect(() => {
    currentScopeKeyRef.current = currentScopeKey;
  }, [currentScopeKey]);
  const isCurrentScope = (scopeKey: string) =>
    currentScopeKeyRef.current === scopeKey;
  const isCurrentLoadRequest = (requestId: number, scopeKey: string) =>
    requestId === loadRequestIdRef.current && isCurrentScope(scopeKey);
  const scope = {
    agentName: effectiveScopeAgentName,
    executionScope: selectedExecutionScope,
  };
  const [activeDialog, setActiveDialog] = useState<{
    integration: Integration;
    SetupComponent: NonNullable<IntegrationProvider["SetupComponent"]>;
  } | null>(null);
  const [configDialog, setConfigDialog] = useState<{
    service: string;
    displayName: string;
    description: string;
    configFields: any[];
    isEditing?: boolean;
    docsUrl?: string | null;
    helperText?: string | null;
    useSelectedScope?: boolean;
  } | null>(null);
  const [filterMode, setFilterMode] = useState<FilterMode>("all");
  const [searchTerm, setSearchTerm] = useState("");
  const { toast } = useToast();

  useEffect(() => {
    if (
      scopeAgentName &&
      !scopedAgents.some((agent) => agent.id === scopeAgentName)
    ) {
      setScopeAgentName(null);
    }
  }, [scopeAgentName, scopedAgents]);

  const loadIntegrations = async (forceRefresh = false) => {
    const scopeKey = currentScopeKey;
    if (!isCurrentScope(scopeKey)) {
      return;
    }

    const requestId = ++loadRequestIdRef.current;
    setLoading(true);
    try {
      // Refetch tools after a setup dialog saved credentials, so their statuses update
      if (forceRefresh) {
        await refetchTools();
        // Return early since refetchTools will trigger this useEffect again via backendTools update
        return;
      }

      const sharedProviderIds = providersSharedByTools(backendTools);
      // The tool named after a provider stands for its card, else the provider's only tool.
      const providerToolFor = (providerId: string) =>
        backendTools.find((tool) => tool.name === providerId) ??
        (sharedProviderIds.has(providerId)
          ? undefined
          : backendTools.find((tool) => tool.auth_provider === providerId));
      // Built-in cards first, then providers discovered from tool metadata.
      const providerIds = new Set([
        ...builtInCardsById.keys(),
        ...backendTools
          .map((tool) => tool.auth_provider)
          .filter((providerId): providerId is string => Boolean(providerId)),
      ]);

      const loadedIntegrations = await Promise.all(
        [...providerIds]
          .filter(
            (providerId) =>
              !hidesSharedOnlyIntegrations ||
              !SHARED_ONLY_PROVIDER_IDS.has(providerId),
          )
          .map(async (providerId) => {
            const providerTool = providerToolFor(providerId);
            const card =
              builtInCardsById.get(providerId) ??
              discoveredOAuthCard(providerId, providerTool);
            const status =
              await providerForIntegration(card)!.loadStatus(scope);
            return withProviderState(card, providerTool, status);
          }),
      );

      // Tool cards for every tool no provider card stands for
      const backendIntegrations = backendTools
        .filter(
          (tool) =>
            !providerIds.has(tool.name) &&
            (!tool.auth_provider || sharedProviderIds.has(tool.auth_provider)),
        )
        .filter(
          (tool) =>
            !hidesSharedOnlyIntegrations ||
            tool.execution_scope_supported !== false,
        )
        .map((tool): Integration => {
          const mapped = mapToolToIntegration(tool);
          return {
            ...mapped,
            icon: getIconForTool(tool.icon, tool.icon_color),
            // Tools with auth_provider show as connected if their status is 'available'
            status:
              tool.auth_provider && tool.status === "available"
                ? "connected"
                : mapped.status,
            auth_provider: tool.auth_provider ?? undefined,
            config_service: tool.name,
          };
        });

      if (!isCurrentLoadRequest(requestId, scopeKey)) {
        return;
      }

      setIntegrationsState({
        scopeKey,
        integrations: [...loadedIntegrations, ...backendIntegrations],
      });
    } catch (error) {
      if (!isCurrentLoadRequest(requestId, scopeKey)) {
        return;
      }
      console.error("Failed to load integrations:", error);
      toast({
        title: "Error",
        description: "Failed to load integrations",
        variant: "destructive",
      });
    } finally {
      if (isCurrentLoadRequest(requestId, scopeKey)) {
        setLoading(false);
      }
    }
  };

  const reloadIntegrations = useEffectEvent(loadIntegrations);
  useEffect(() => {
    reloadIntegrations();
  }, [backendTools, currentScopeKey]);

  // OAuth cards connect for the selected agent's scope; other credentials stay shared-only.
  const blocksScopedDashboardCredentials = (integration: Integration) =>
    disablesDashboardCredentialManagement &&
    (integration.setup_type !== "none" ||
      Boolean(integration.auth_provider) ||
      hasConfigFields(integration)) &&
    !integration.oauth_provider_id;

  const openToolConfigDialog = (integration: Integration) => {
    const configFields = integration.config_fields;
    if (!configFields?.length) {
      return false;
    }
    setConfigDialog({
      service: integration.config_service ?? integration.id,
      displayName: integration.name,
      description: integration.description,
      configFields,
      isEditing: integration.status === "connected",
      docsUrl: integration.docs_url || null,
      helperText: integration.helper_text || null,
    });
    return true;
  };

  const openOAuthClientConfigDialog = (integration: Integration) => {
    if (!integration.oauth_client_config_service) {
      return false;
    }
    const hasCustomClientConfig =
      integration.oauth_custom_client_configured === true;
    const configFields: ToolFieldSchema[] = [
      {
        name: "client_id",
        label: "Client ID",
        type: "text",
        required: true,
        placeholder: "OAuth app client ID",
        description: "OAuth app client ID",
      },
      {
        name: "client_secret",
        label: "Client Secret",
        type: "password",
        required: !hasCustomClientConfig,
        requiredWhenFieldChanges: hasCustomClientConfig ? "client_id" : null,
        placeholder: hasCustomClientConfig
          ? "Required when changing Client ID"
          : "OAuth app client secret",
        description: hasCustomClientConfig
          ? "The saved secret is kept for edits that do not change the Client ID."
          : "OAuth app client secret",
      },
    ];
    if (integration.oauth_client_redirect_uri_supported === true) {
      configFields.push({
        name: "redirect_uri",
        label: "Redirect URI",
        type: "text",
        required: false,
        placeholder: integration.oauth_default_redirect_uri,
        description: "Optional provider-specific redirect URI",
      });
    }
    setConfigDialog({
      service: integration.oauth_client_config_service,
      displayName: `${integration.name} OAuth Client`,
      description: `Configure the OAuth app client used to connect ${integration.name}.`,
      configFields,
      isEditing: hasCustomClientConfig,
      docsUrl: integration.docs_url || null,
      helperText: null,
      useSelectedScope: false,
    });
    return true;
  };

  const handleIntegrationAction = async (integration: Integration) => {
    if (blocksScopedDashboardCredentials(integration)) {
      toast({
        title: "Shared-only dashboard configuration",
        description:
          "Dashboard credential setup is only supported for shared deployment credentials.",
        variant: "destructive",
      });
      return;
    }

    const provider = providerForIntegration(integration);
    if (provider) {
      if (provider.SetupComponent) {
        setActiveDialog({
          integration,
          SetupComponent: provider.SetupComponent,
        });
        return;
      }

      if (
        integration.status === "connected" &&
        openToolConfigDialog(integration)
      ) {
        return;
      }

      if (
        integration.oauth_client_configured === false &&
        openOAuthClientConfigDialog(integration)
      ) {
        return;
      }

      if (provider.connect) {
        setLoading(true);
        try {
          await provider.connect(scope);
          await loadIntegrations(); // Reload status
        } catch (error) {
          toast({
            title: "Action Failed",
            description:
              error instanceof Error
                ? error.message
                : "Failed to perform action",
            variant: "destructive",
          });
        } finally {
          setLoading(false);
        }
      }
    } else if (!openToolConfigDialog(integration)) {
      toast({
        title: "Configuration Error",
        description: `${integration.name} requires configuration but no fields are specified.`,
        variant: "destructive",
      });
    }
  };

  const runDisconnectOperation = async (
    integration: Integration,
    disconnect: () => Promise<DisconnectToast | undefined>,
  ) => {
    if (blocksScopedDashboardCredentials(integration)) {
      toast({
        title: "Shared-only dashboard configuration",
        description:
          "Dashboard credential editing is only supported for shared deployment credentials.",
        variant: "destructive",
      });
      return;
    }

    setLoading(true);
    try {
      const result = await disconnect();

      // Refetch tools to update status
      await refetchTools();

      toast(
        result ?? {
          title: "Disconnected",
          description: `${integration.name} has been disconnected.`,
        },
      );
    } catch (error) {
      toast({
        title: "Disconnect Failed",
        description:
          error instanceof Error ? error.message : "Failed to disconnect",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const handleStoredCredentialDelete = (integration: Integration) =>
    runDisconnectOperation(integration, async () => {
      const response = await fetch(
        scopedApiUrl(
          `/api/credentials/${integration.config_service ?? integration.id}`,
          scope,
        ),
        {
          method: "DELETE",
        },
      );

      if (!response.ok) {
        throw new Error(
          await apiErrorMessageFromResponse(response, "Failed to disconnect"),
        );
      }
      // The environment or a granted shared credential can still supply deleted credentials, so the server says what happened.
      const { message } = (await response.json()) as { message: string };
      return { title: integration.name, description: message };
    });

  const integrations =
    integrationsState.scopeKey === currentScopeKey
      ? integrationsState.integrations
      : NO_INTEGRATIONS;

  const getActionButton = (integration: Integration) => {
    if (blocksScopedDashboardCredentials(integration)) {
      return (
        <Button disabled variant="outline" size="sm">
          Shared-only config
        </Button>
      );
    }

    const provider = providerForIntegration(integration);
    const disconnectProvider = provider
      ? () =>
          runDisconnectOperation(integration, async () => {
            await provider.disconnect(scope);
            return integration.oauth_reset_required === true
              ? {
                  title: "Connection reset",
                  description: `${integration.name} OAuth has been reset.`,
                }
              : undefined;
          })
      : undefined;

    const canConfigureOAuthClient =
      integration.setup_type === "oauth" &&
      integration.oauth_client_configured === true &&
      integration.oauth_service_account_configured !== true &&
      Boolean(integration.oauth_client_config_service);
    const oauthClientConfigButton = canConfigureOAuthClient ? (
      <Button
        onClick={() => openOAuthClientConfigDialog(integration)}
        disabled={loading}
        variant="outline"
        size="sm"
      >
        <Settings className="h-4 w-4 mr-1" />
        {integration.oauth_custom_client_configured === true
          ? "Edit client"
          : "Use custom client"}
      </Button>
    ) : null;
    const manualFallbackAvailable = hasManualOAuthFallback(integration);
    const manualFallbackLabel = manualOAuthFallbackLabel(integration);
    const manualFallbackActionLabel = manualFallbackLabel.toLocaleLowerCase();
    const manualFallbackButton =
      manualFallbackAvailable && !hasConfiguredOAuthFallback(integration) ? (
        <Button
          onClick={() => openToolConfigDialog(integration)}
          disabled={loading}
          variant="outline"
          size="sm"
        >
          <Key className="h-4 w-4 mr-1" />
          Use {manualFallbackActionLabel}
        </Button>
      ) : null;
    const configuredFallbackControls =
      manualFallbackAvailable &&
      integration.environment_auth_configured === true &&
      integration.manual_auth_configured !== true ? (
        <>
          <SuccessBadge icon={Key}>
            Environment {manualFallbackLabel}
          </SuccessBadge>
          <Button
            onClick={() => openToolConfigDialog(integration)}
            disabled={loading}
            variant="outline"
            size="sm"
          >
            Configure
          </Button>
        </>
      ) : manualFallbackAvailable &&
        integration.manual_auth_configured === true ? (
        <>
          <SuccessBadge icon={Key}>{manualFallbackLabel}</SuccessBadge>
          <Button
            onClick={() => openToolConfigDialog(integration)}
            disabled={loading}
            variant="outline"
            size="sm"
          >
            Edit {manualFallbackActionLabel}
          </Button>
          <Button
            onClick={() => handleStoredCredentialDelete(integration)}
            disabled={loading}
            variant="destructive"
            size="sm"
          >
            Remove {manualFallbackActionLabel}
          </Button>
        </>
      ) : null;

    const buttonText =
      integration.setup_type === "special"
        ? "Setup"
        : integration.setup_type === "oauth"
          ? "Connect"
          : "Configure";
    const icon =
      integration.setup_type === "special" ? (
        <Settings className="h-4 w-4" />
      ) : integration.setup_type === "oauth" ? (
        <ExternalLink className="h-4 w-4" />
      ) : (
        <Key className="h-4 w-4" />
      );
    const actionButton = (
      <Button
        onClick={() => handleIntegrationAction(integration)}
        disabled={loading}
        size="sm"
        className="flex items-center gap-2"
      >
        {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
        {manualFallbackAvailable
          ? `Connect with ${integration.name}`
          : buttonText}
      </Button>
    );

    // Tools whose authentication comes from a provider card
    if (integration.auth_provider) {
      const authProvider = integrations.find(
        (i) => i.id === integration.auth_provider,
      );
      if (
        integration.status !== "connected" &&
        authProvider?.status !== "connected"
      ) {
        return (
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-muted-foreground">
              Requires {authProvider?.name || integration.auth_provider}
            </Badge>
            <Button
              onClick={() => {
                if (authProvider) {
                  handleIntegrationAction(authProvider);
                } else {
                  toast({
                    title: `Connect ${integration.auth_provider} first`,
                    description: `Please connect to ${integration.auth_provider} to use this tool.`,
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
      if (!hasConfigFields(integration)) {
        return <SuccessBadge icon={CheckCircle2}>Connected</SuccessBadge>;
      }
      return (
        <div className="flex gap-2 items-center">
          <SuccessBadge icon={CheckCircle2}>Connected</SuccessBadge>
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

    // Tools with no setup required, whose config fields are optional
    if (integration.setup_type === "none") {
      if (!hasConfigFields(integration)) {
        return <SuccessBadge icon={CheckCircle2}>Ready to Use</SuccessBadge>;
      }
      return (
        <div className="flex gap-2 items-center">
          <SuccessBadge icon={CheckCircle2}>Ready</SuccessBadge>
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

    if (
      integration.setup_type === "oauth" &&
      integration.oauth_reset_required === true
    ) {
      return (
        <div className="flex gap-2 items-center">
          <Button
            onClick={disconnectProvider}
            disabled={loading || !disconnectProvider}
            variant="destructive"
            size="sm"
          >
            {loading && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
            Reset connection
          </Button>
          {oauthClientConfigButton}
          {configuredFallbackControls ?? manualFallbackButton}
        </div>
      );
    }

    if (configuredFallbackControls) {
      return (
        <div className="flex gap-2 items-center">
          {configuredFallbackControls}
          {oauthClientConfigButton}
        </div>
      );
    }

    if (integration.setup_type === "oauth" && integration.status_error) {
      return (
        <div className="flex gap-2 items-center">
          <Button disabled variant="outline" size="sm">
            Status unavailable
          </Button>
          {oauthClientConfigButton}
          {manualFallbackButton}
          <Button
            onClick={() => void loadIntegrations()}
            disabled={loading}
            variant="outline"
            size="sm"
          >
            Retry status
          </Button>
        </div>
      );
    }

    if (integration.status !== "connected" && canConfigureOAuthClient) {
      return (
        <div className="flex gap-2 items-center">
          {actionButton}
          {oauthClientConfigButton}
          {manualFallbackButton}
        </div>
      );
    }

    if (
      integration.setup_type === "oauth" &&
      integration.oauth_client_configured === false &&
      integration.oauth_service_account_configured !== true
    ) {
      return (
        <div className="flex gap-2 items-center">
          <Button
            onClick={() => handleIntegrationAction(integration)}
            disabled={loading || !integration.oauth_client_config_service}
            variant="outline"
            size="sm"
          >
            Configure client
          </Button>
          {manualFallbackButton}
        </div>
      );
    }

    if (integration.status === "connected" && manualFallbackAvailable) {
      return (
        <div className="flex gap-2">
          {manualFallbackButton}
          {oauthClientConfigButton}
          <Button
            onClick={disconnectProvider}
            disabled={loading || !disconnectProvider}
            variant="destructive"
            size="sm"
          >
            Disconnect
          </Button>
        </div>
      );
    }

    // For other connected tools, show Edit/Disconnect
    if (integration.status === "connected") {
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
          {oauthClientConfigButton}
          <Button
            onClick={
              disconnectProvider ??
              (() => handleStoredCredentialDelete(integration))
            }
            disabled={loading}
            variant="destructive"
            size="sm"
          >
            Disconnect
          </Button>
        </div>
      );
    }

    return manualFallbackButton ? (
      <div className="flex gap-2 items-center">
        {actionButton}
        {manualFallbackButton}
      </div>
    ) : (
      actionButton
    );
  };

  const filteredIntegrations = useMemo(() => {
    let filtered = integrations;

    // Filter by mode
    switch (filterMode) {
      case "available":
        filtered = filtered.filter(
          (i) => i.status === "available" || i.status === "connected",
        );
        break;
      case "unconfigured":
        filtered = filtered.filter(
          (i) => i.status !== "connected" && i.setup_type !== "none",
        );
        break;
      case "configured":
        filtered = filtered.filter((i) => i.status === "connected");
        break;
      // 'all' - no filtering needed
    }

    // Filter by search term
    if (searchTerm) {
      filtered = filtered.filter(
        (i) =>
          i.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
          i.description.toLowerCase().includes(searchTerm.toLowerCase()),
      );
    }

    return filtered;
  }, [integrations, filterMode, searchTerm]);

  const categories = useMemo(() => {
    const allCategories = [
      { id: "all", name: "All", integrations: filteredIntegrations },
      ...CATEGORIES.map((category) => ({
        ...category,
        integrations: filteredIntegrations.filter(
          (i) => i.category === category.id,
        ),
      })),
    ];

    return filterMode !== "all"
      ? allCategories.filter((cat) => cat.integrations.length > 0)
      : allCategories;
  }, [filteredIntegrations, filterMode]);

  // Show loading state while fetching tools
  if (toolsLoading && integrations.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4" />
          <p className="text-gray-600 dark:text-gray-400">
            Loading available tools...
          </p>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="h-full overflow-y-auto">
        <div className="mb-4">
          <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
            <h2 className="text-2xl font-bold">Tools</h2>
            <div className="flex flex-wrap items-center gap-2 w-full sm:w-auto">
              <Select
                value={scopeAgentName ?? "shared"}
                onValueChange={(value) =>
                  setScopeAgentName(value === "shared" ? null : value)
                }
              >
                <SelectTrigger className="w-full sm:w-72">
                  <SelectValue placeholder="Shared deployment credentials" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="shared">
                    Shared deployment credentials
                  </SelectItem>
                  {scopedAgents.map((agent) => (
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
                onChange={(e) => setSearchTerm(e.target.value)}
                className="w-full sm:w-64"
              />
              <FilterSelector
                options={[
                  { value: "all", label: "Show All" },
                  { value: "available", label: "Available" },
                  { value: "unconfigured", label: "Unconfigured" },
                  { value: "configured", label: "Configured" },
                ]}
                value={filterMode}
                onChange={(value) => setFilterMode(value as FilterMode)}
                size="sm"
              />
            </div>
          </div>
          <p className="text-gray-600 dark:text-gray-400">
            {selectedScopeAgent
              ? `Configuring tools for ${selectedScopeAgent.display_name} (${
                  selectedScopeLabel ?? "unscoped"
                }).`
              : "Connect external services to enable agent capabilities"}
          </p>
          {hidesSharedOnlyIntegrations && (
            <Alert className="mt-3">
              <AlertDescription>
                Legacy dashboard credential setup, editing, and disconnect are
                only supported for shared deployment credentials.
              </AlertDescription>
              <AlertDescription className="mt-2">
                Home Assistant and Spotify remain shared-only unless the agent
                has an effective shared runtime scope (
                <code>worker_scope=shared</code>). OAuth-backed integrations can
                be connected for this selected agent.
              </AlertDescription>
            </Alert>
          )}
          {selectedScopeAgent && statusAuthoritative === false && (
            <Alert className="mt-3">
              <AlertDescription>
                Requester-scoped tool status is preview only. The dashboard can
                show scope support rules and shared env-backed availability, but
                it cannot inspect live requester-owned scoped credentials.
              </AlertDescription>
            </Alert>
          )}
        </div>

        <div className="">
          <Tabs defaultValue="all" className="h-full">
            <TabsList className="flex flex-wrap h-auto gap-1 overflow-visible">
              {categories.map((category) => (
                <TabsTrigger
                  key={category.id}
                  value={category.id}
                  className="text-xs flex-shrink-0"
                >
                  {category.name}
                  {category.integrations.length > 0 && (
                    <Badge variant="secondary" className="ml-1 text-xs">
                      {category.integrations.length}
                    </Badge>
                  )}
                </TabsTrigger>
              ))}
            </TabsList>

            {categories.map((category) => (
              <TabsContent
                key={category.id}
                value={category.id}
                className="mt-4"
              >
                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                  {category.integrations.map((integration) => (
                    <IntegrationCard
                      key={integration.id}
                      integration={integration}
                      action={getActionButton(integration)}
                    />
                  ))}
                </div>
              </TabsContent>
            ))}
          </Tabs>
        </div>
      </div>

      {/* Provider setup dialog */}
      {activeDialog && (
        <Dialog
          open={true}
          onOpenChange={(open) => !open && setActiveDialog(null)}
        >
          <DialogContent className="max-w-4xl max-h-[90vh] overflow-auto">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                {activeDialog.integration.icon}
                {activeDialog.integration.name} Setup
              </DialogTitle>
              <DialogDescription>
                {activeDialog.integration.description}
              </DialogDescription>
            </DialogHeader>
            <activeDialog.SetupComponent
              {...scope}
              onSuccess={async () => {
                setActiveDialog(null);
                await loadIntegrations(true);
              }}
            />
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
          agentName={
            configDialog.useSelectedScope === false
              ? null
              : effectiveScopeAgentName
          }
          executionScope={
            configDialog.useSelectedScope === false
              ? null
              : selectedExecutionScope
          }
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
