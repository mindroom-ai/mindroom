import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Eye,
  EyeOff,
  FlaskConical,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from 'lucide-react';
import { API_ENDPOINTS, fetchJSON } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/use-toast';

interface CredentialStatusResponse {
  service: string;
  has_credentials: boolean;
  key_names?: string[] | null;
}

interface CredentialGetResponse {
  service: string;
  credentials: Record<string, unknown>;
}

interface ServiceStatus {
  service: string;
  hasCredentials: boolean;
  keyNames: string[];
}

const EMPTY_JSON = '{}';
const SERVICE_NAME_PATTERN = /^[a-zA-Z0-9:_-]+$/;
const PARTIAL_STATUS_ERROR =
  'Some service statuses could not be loaded. You can still edit credentials.';

function formatServiceJson(credentials: Record<string, unknown>): string {
  if (Object.keys(credentials).length === 0) {
    return EMPTY_JSON;
  }
  return JSON.stringify(credentials, null, 2);
}

function normalizeStatus(status: CredentialStatusResponse): ServiceStatus {
  return {
    service: status.service,
    hasCredentials: status.has_credentials,
    keyNames: status.key_names ?? [],
  };
}

function validateServiceName(service: string): string | null {
  if (!service.trim()) {
    return 'Service name is required';
  }
  if (!SERVICE_NAME_PATTERN.test(service)) {
    return 'Service name can only include letters, numbers, colon, underscore, and hyphen';
  }
  return null;
}

export function Credentials() {
  const { toast } = useToast();

  const [services, setServices] = useState<ServiceStatus[]>([]);
  const [selectedService, setSelectedService] = useState('');
  const [newServiceName, setNewServiceName] = useState('');
  const [jsonDraft, setJsonDraft] = useState(EMPTY_JSON);
  const [loadedDraft, setLoadedDraft] = useState(EMPTY_JSON);
  const [isLoadingServices, setIsLoadingServices] = useState(true);
  const [isLoadingCredentials, setIsLoadingCredentials] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [isJsonVisible, setIsJsonVisible] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latestCredentialsRequestRef = useRef(0);
  const selectedServiceRef = useRef('');

  const sortedServices = useMemo(
    () => [...services].sort((a, b) => a.service.localeCompare(b.service)),
    [services]
  );
  const hasUnsavedChanges = Boolean(selectedService) && jsonDraft !== loadedDraft;

  useEffect(() => {
    selectedServiceRef.current = selectedService;
  }, [selectedService]);

  useEffect(() => {
    setIsJsonVisible(false);
  }, [selectedService]);

  const loadServices = useCallback(async () => {
    setIsLoadingServices(true);
    try {
      const serviceNames = await fetchJSON<string[]>(API_ENDPOINTS.credentials.list);
      const statusResults = await Promise.allSettled(
        serviceNames.map(service =>
          fetchJSON<CredentialStatusResponse>(API_ENDPOINTS.credentials.status(service))
        )
      );
      const statuses = serviceNames.map((service, index) => {
        const result = statusResults[index];
        if (result.status === 'fulfilled') {
          return normalizeStatus(result.value);
        }
        return {
          service,
          hasCredentials: false,
          keyNames: [],
        };
      });
      setServices(statuses);
      if (statusResults.some(result => result.status === 'rejected')) {
        setError(PARTIAL_STATUS_ERROR);
      } else {
        setError(null);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load credential services';
      setError(message);
    } finally {
      setIsLoadingServices(false);
    }
  }, []);

  const loadCredentials = useCallback(async (service: string) => {
    const requestId = latestCredentialsRequestRef.current + 1;
    latestCredentialsRequestRef.current = requestId;
    setIsLoadingCredentials(true);
    setError(null);
    try {
      const data = await fetchJSON<CredentialGetResponse>(API_ENDPOINTS.credentials.get(service));
      if (
        requestId !== latestCredentialsRequestRef.current ||
        selectedServiceRef.current !== service
      ) {
        return;
      }
      const formatted = formatServiceJson(data.credentials ?? {});
      setJsonDraft(formatted);
      setLoadedDraft(formatted);
    } catch (err) {
      if (
        requestId !== latestCredentialsRequestRef.current ||
        selectedServiceRef.current !== service
      ) {
        return;
      }
      const message = err instanceof Error ? err.message : 'Failed to load credentials';
      setError(message);
      setJsonDraft(EMPTY_JSON);
      setLoadedDraft(EMPTY_JSON);
    } finally {
      if (
        requestId === latestCredentialsRequestRef.current &&
        selectedServiceRef.current === service
      ) {
        setIsLoadingCredentials(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadServices();
  }, [loadServices]);

  useEffect(() => {
    if (sortedServices.length === 0) {
      latestCredentialsRequestRef.current += 1;
      setSelectedService('');
      setJsonDraft(EMPTY_JSON);
      setLoadedDraft(EMPTY_JSON);
      setIsLoadingCredentials(false);
      return;
    }
    if (!selectedService || !sortedServices.some(service => service.service === selectedService)) {
      setSelectedService(sortedServices[0].service);
    }
  }, [selectedService, sortedServices]);

  useEffect(() => {
    if (!selectedService) {
      return;
    }
    void loadCredentials(selectedService);
  }, [loadCredentials, selectedService]);

  const handleSelectService = useCallback(
    (service: string) => {
      if (service === selectedService) {
        return;
      }
      if (
        hasUnsavedChanges &&
        !window.confirm(`Discard unsaved changes for '${selectedService}'?`)
      ) {
        return;
      }
      setSelectedService(service);
    },
    [hasUnsavedChanges, selectedService]
  );

  const handleCreateService = useCallback(() => {
    const candidate = newServiceName.trim();
    const validationError = validateServiceName(candidate);
    if (validationError) {
      setError(validationError);
      return;
    }
    if (services.some(service => service.service === candidate)) {
      setError(`Service '${candidate}' already exists`);
      setSelectedService(candidate);
      return;
    }
    setError(null);
    setServices(previous => [
      ...previous,
      {
        service: candidate,
        hasCredentials: false,
        keyNames: [],
      },
    ]);
    setSelectedService(candidate);
    setJsonDraft(EMPTY_JSON);
    setLoadedDraft(EMPTY_JSON);
    setNewServiceName('');
  }, [newServiceName, services]);

  const refreshSelectedStatus = useCallback(async (service: string) => {
    try {
      const status = await fetchJSON<CredentialStatusResponse>(
        API_ENDPOINTS.credentials.status(service)
      );
      setServices(previous => {
        const withoutCurrent = previous.filter(item => item.service !== service);
        return [...withoutCurrent, normalizeStatus(status)];
      });
    } catch {
      // Keep existing list if status refresh fails.
    }
  }, []);

  const handleSave = useCallback(async () => {
    if (!selectedService) {
      return;
    }

    const validationError = validateServiceName(selectedService);
    if (validationError) {
      setError(validationError);
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(jsonDraft);
    } catch {
      setError('Credentials must be valid JSON');
      return;
    }

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setError('Credentials must be a JSON object');
      return;
    }

    setIsSaving(true);
    setError(null);
    try {
      await fetchJSON(API_ENDPOINTS.credentials.set(selectedService), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credentials: parsed }),
      });
      const formatted = formatServiceJson(parsed as Record<string, unknown>);
      setJsonDraft(formatted);
      setLoadedDraft(formatted);
      await refreshSelectedStatus(selectedService);
      toast({
        title: 'Credentials saved',
        description: `Updated credentials for '${selectedService}'.`,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to save credentials';
      setError(message);
      toast({
        title: 'Save failed',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setIsSaving(false);
    }
  }, [jsonDraft, refreshSelectedStatus, selectedService, toast]);

  const handleDelete = useCallback(async () => {
    if (!selectedService) {
      return;
    }
    if (!window.confirm(`Delete credentials for '${selectedService}'?`)) {
      return;
    }
    setIsDeleting(true);
    setError(null);
    try {
      await fetchJSON(API_ENDPOINTS.credentials.delete(selectedService), {
        method: 'DELETE',
      });
      setServices(previous => previous.filter(service => service.service !== selectedService));
      setSelectedService('');
      setJsonDraft(EMPTY_JSON);
      setLoadedDraft(EMPTY_JSON);
      toast({
        title: 'Credentials deleted',
        description: `Removed credentials for '${selectedService}'.`,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to delete credentials';
      setError(message);
      toast({
        title: 'Delete failed',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setIsDeleting(false);
    }
  }, [selectedService, toast]);

  const handleTest = useCallback(async () => {
    if (!selectedService) {
      return;
    }
    setIsTesting(true);
    setError(null);
    try {
      const response = await fetchJSON<{ message?: string }>(
        API_ENDPOINTS.credentials.test(selectedService),
        {
          method: 'POST',
        }
      );
      toast({
        title: 'Credentials check',
        description: response.message ?? `Credentials exist for '${selectedService}'.`,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Credentials test failed';
      setError(message);
      toast({
        title: 'Test failed',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setIsTesting(false);
    }
  }, [selectedService, toast]);

  const handleRefresh = useCallback(async () => {
    if (
      selectedService &&
      hasUnsavedChanges &&
      !window.confirm(`Discard unsaved changes for '${selectedService}' and refresh?`)
    ) {
      return;
    }
    setError(null);
    await loadServices();
    if (selectedService) {
      await loadCredentials(selectedService);
    }
  }, [hasUnsavedChanges, loadCredentials, loadServices, selectedService]);

  const handleCopy = useCallback(async () => {
    if (!selectedService) {
      return;
    }
    await navigator.clipboard?.writeText(jsonDraft);
    toast({
      title: 'Copied credentials JSON',
      description: 'Copied in cleartext. Handle clipboard contents carefully.',
    });
  }, [jsonDraft, selectedService, toast]);

  return (
    <div className="h-full overflow-y-auto overflow-x-hidden">
      <div className="h-full flex flex-col gap-4">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-xl">Credentials Manager</CardTitle>
            <CardDescription>
              Manage raw credential payloads by service name (for tools, model aliases, and private
              integrations).
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">Services: {sortedServices.length}</Badge>
              {selectedService ? <Badge variant="default">Active: {selectedService}</Badge> : null}
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)] gap-4">
              <Card className="border border-border/60 h-full flex flex-col">
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Services</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3 h-full min-h-0 flex flex-col">
                  <div className="flex gap-2">
                    <Input
                      value={newServiceName}
                      onChange={event => setNewServiceName(event.target.value)}
                      placeholder="new_service_name"
                    />
                    <Button variant="outline" onClick={handleCreateService}>
                      <Plus className="h-4 w-4 mr-1" />
                      Add
                    </Button>
                  </div>

                  <div className="space-y-1 overflow-auto pr-1 flex-1 min-h-0">
                    {isLoadingServices ? (
                      <p className="text-sm text-muted-foreground">Loading services...</p>
                    ) : sortedServices.length === 0 ? (
                      <p className="text-sm text-muted-foreground">
                        No services found yet. Add one and save JSON credentials.
                      </p>
                    ) : (
                      sortedServices.map(service => {
                        const isActive = service.service === selectedService;
                        return (
                          <button
                            key={service.service}
                            type="button"
                            onClick={() => handleSelectService(service.service)}
                            className={cn(
                              'w-full rounded-md border p-2 text-left transition-colors',
                              isActive
                                ? 'border-primary bg-primary/5'
                                : 'border-border hover:border-primary/40 hover:bg-muted/40'
                            )}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <code className="text-xs font-medium">{service.service}</code>
                              <Badge variant={service.hasCredentials ? 'default' : 'secondary'}>
                                {service.hasCredentials ? 'Configured' : 'Empty'}
                              </Badge>
                            </div>
                            {service.keyNames.length > 0 ? (
                              <p className="mt-1 text-xs text-muted-foreground truncate">
                                Keys: {service.keyNames.join(', ')}
                              </p>
                            ) : null}
                          </button>
                        );
                      })
                    )}
                  </div>
                </CardContent>
              </Card>

              <Card className="border border-border/60">
                <CardHeader className="pb-2">
                  <CardTitle className="text-base flex items-center gap-2">
                    <CheckCircle2 className="h-4 w-4" />
                    Credential Payload
                  </CardTitle>
                  <CardDescription>
                    {selectedService ? (
                      <>
                        Editing <code>{selectedService}</code>
                      </>
                    ) : (
                      'Select or create a service to edit credentials'
                    )}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="flex items-start gap-2 rounded-md border border-amber-300/70 bg-amber-50/80 p-2 text-xs text-amber-900 dark:border-amber-700/60 dark:bg-amber-900/20 dark:text-amber-200">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                    <p>
                      Credentials are hidden by default for safer screen sharing. Showing or copying
                      exposes cleartext secrets.
                    </p>
                  </div>
                  <div className="flex justify-end">
                    <Button
                      variant="outline"
                      onClick={() => setIsJsonVisible(previous => !previous)}
                      disabled={!selectedService || isLoadingCredentials}
                    >
                      {isJsonVisible ? (
                        <EyeOff className="h-4 w-4 mr-2" />
                      ) : (
                        <Eye className="h-4 w-4 mr-2" />
                      )}
                      {isJsonVisible ? 'Hide' : 'Show'}
                    </Button>
                  </div>
                  <div className="relative">
                    <Textarea
                      value={jsonDraft}
                      onChange={event => setJsonDraft(event.target.value)}
                      disabled={!selectedService || isLoadingCredentials}
                      className={cn(
                        'font-mono min-h-[320px] transition-[filter] duration-150',
                        selectedService &&
                          !isJsonVisible &&
                          'blur-sm pointer-events-none select-none'
                      )}
                      placeholder='{"api_key":"..."}'
                    />
                    {selectedService && !isJsonVisible ? (
                      <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-md bg-background/10">
                        <p className="rounded-md border bg-background/90 px-3 py-1 text-xs text-muted-foreground shadow-sm">
                          Credentials hidden for screen sharing. Click Show to reveal.
                        </p>
                      </div>
                    ) : null}
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <Button onClick={handleSave} disabled={!selectedService || isSaving}>
                      <Save className="h-4 w-4 mr-2" />
                      {isSaving ? 'Saving...' : 'Save'}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={handleTest}
                      disabled={!selectedService || isTesting}
                    >
                      <FlaskConical className="h-4 w-4 mr-2" />
                      {isTesting ? 'Testing...' : 'Test'}
                    </Button>
                    <Button variant="outline" onClick={handleRefresh}>
                      <RefreshCw className="h-4 w-4 mr-2" />
                      Refresh
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => void handleCopy()}
                      disabled={!selectedService}
                    >
                      <Copy className="h-4 w-4 mr-2" />
                      Copy JSON
                    </Button>
                    <Button
                      variant="destructive"
                      onClick={handleDelete}
                      disabled={!selectedService || isDeleting}
                    >
                      <Trash2 className="h-4 w-4 mr-2" />
                      {isDeleting ? 'Deleting...' : 'Delete'}
                    </Button>
                  </div>
                </CardContent>
              </Card>
            </div>
          </CardContent>
        </Card>

        {error ? (
          <Card className="border-destructive/30">
            <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
          </Card>
        ) : null}
      </div>
    </div>
  );
}
