import { useState, useEffect } from 'react';
import { Mic, Settings, Volume2, Info } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { useToast } from '@/components/ui/use-toast';
import { useConfigStore } from '@/store/configStore';
import { VoiceConfig as VoiceConfigType } from '@/types/config';

const OPENAI_TRANSCRIPTION_ENDPOINT = 'https://api.openai.com/v1/audio/transcriptions';

const DEFAULT_VOICE_CONFIG: VoiceConfigType = {
  enabled: false,
  stt: {
    provider: 'openai',
    model: 'whisper-1',
    api_key: '',
    host: '',
  },
  intelligence: {
    model: 'default',
  },
};

const STT_PROVIDER_LABELS: Record<string, string> = {
  openai: 'OpenAI API (Cloud)',
  custom: 'OpenAI-compatible Endpoint',
};

function mergeVoiceConfig(config?: Partial<VoiceConfigType>): VoiceConfigType {
  return {
    ...DEFAULT_VOICE_CONFIG,
    ...config,
    stt: {
      ...DEFAULT_VOICE_CONFIG.stt,
      ...(config?.stt || {}),
    },
    intelligence: {
      ...DEFAULT_VOICE_CONFIG.intelligence,
      ...(config?.intelligence || {}),
    },
  };
}

function normalizeHost(host?: string): string {
  if (!host) return '';
  return host.trim().replace(/\/+$/, '');
}

export function VoiceConfig() {
  const { config, saveConfig, markDirty } = useConfigStore();
  const { toast } = useToast();

  // Initialize local state with default values if voice config doesn't exist
  const [voiceConfig, setVoiceConfig] = useState<VoiceConfigType>(() =>
    mergeVoiceConfig(config?.voice)
  );

  // Update local state when config changes
  useEffect(() => {
    setVoiceConfig(mergeVoiceConfig(config?.voice));
  }, [config?.voice]);

  const handleVoiceConfigChange = (updates: Partial<VoiceConfigType>) => {
    const newConfig = { ...voiceConfig, ...updates };
    setVoiceConfig(newConfig);

    // Update the store
    if (config) {
      config.voice = newConfig;
      markDirty();
    }
  };

  const handleSTTChange = (updates: Partial<VoiceConfigType['stt']>) => {
    handleVoiceConfigChange({
      stt: { ...voiceConfig.stt, ...updates },
    });
  };

  const handleSTTProviderChange = (provider: string) => {
    handleSTTChange({
      provider,
      host: provider === 'openai' ? '' : voiceConfig.stt.host,
    });
  };

  const handleIntelligenceChange = (updates: Partial<VoiceConfigType['intelligence']>) => {
    handleVoiceConfigChange({
      intelligence: { ...voiceConfig.intelligence, ...updates },
    });
  };

  // Get available models from config
  const availableModels = config?.models ? Object.keys(config.models) : [];
  const normalizedHost = normalizeHost(voiceConfig.stt.host);
  const showHostField = voiceConfig.stt.provider === 'custom' || Boolean(normalizedHost);
  const missingHostForCustomProvider = voiceConfig.stt.provider === 'custom' && !normalizedHost;
  const effectiveEndpoint = normalizedHost
    ? `${normalizedHost}/v1/audio/transcriptions`
    : OPENAI_TRANSCRIPTION_ENDPOINT;
  const effectiveMode = normalizedHost ? 'OpenAI-compatible API' : 'OpenAI API';
  const keySource = voiceConfig.stt.api_key?.trim()
    ? 'Stored in voice settings'
    : 'OPENAI_API_KEY environment variable';
  const providerLabel = STT_PROVIDER_LABELS[voiceConfig.stt.provider] || voiceConfig.stt.provider;

  const handleSave = async () => {
    try {
      await saveConfig();
      toast({
        title: 'Voice Configuration Saved',
        description: 'Your voice settings have been updated successfully.',
      });
    } catch (error) {
      toast({
        title: 'Save Failed',
        description: 'Failed to save voice configuration.',
        variant: 'destructive',
      });
    }
  };

  return (
    <div className="space-y-6">
      {/* Main Voice Settings */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Volume2 className="h-5 w-5 text-primary" />
              <CardTitle>Voice Message Support</CardTitle>
            </div>
            <input
              type="checkbox"
              checked={voiceConfig.enabled}
              onChange={e => handleVoiceConfigChange({ enabled: e.target.checked })}
              className="h-5 w-5 rounded"
            />
          </div>
          <CardDescription>
            Enable automatic transcription and processing of voice messages
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-6">
          {/* Status Alert */}
          <Alert>
            <Info className="h-4 w-4" />
            <AlertDescription>
              {voiceConfig.enabled
                ? 'Voice messages will be automatically transcribed and processed. The router agent handles all voice messages to avoid duplicates.'
                : 'Voice message handling is currently disabled. You can still review and edit settings below.'}
            </AlertDescription>
          </Alert>

          {/* Current Settings Summary */}
          <div className="rounded-lg border border-border bg-muted/40 p-4 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold">Current Effective Settings</h3>
              <Badge variant={voiceConfig.enabled ? 'default' : 'secondary'}>
                {voiceConfig.enabled ? 'Enabled' : 'Disabled'}
              </Badge>
            </div>
            <div className="mt-3 space-y-2 text-sm">
              <div className="flex items-start justify-between gap-4">
                <span className="text-muted-foreground">Mode:</span>
                <span className="font-mono text-right text-foreground">{effectiveMode}</span>
              </div>
              <div className="flex items-start justify-between gap-4">
                <span className="text-muted-foreground">Provider Setting:</span>
                <span className="font-mono text-right text-foreground">{providerLabel}</span>
              </div>
              <div className="flex items-start justify-between gap-4">
                <span className="text-muted-foreground">STT Model:</span>
                <span className="font-mono text-right text-foreground">
                  {voiceConfig.stt.model}
                </span>
              </div>
              <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
                <span className="text-muted-foreground">Endpoint:</span>
                <span className="font-mono text-foreground break-all sm:text-right">
                  {effectiveEndpoint}
                </span>
              </div>
              <div className="flex items-start justify-between gap-4">
                <span className="text-muted-foreground">API Key Source:</span>
                <span className="font-mono text-right text-foreground">{keySource}</span>
              </div>
              <div className="flex items-start justify-between gap-4">
                <span className="text-muted-foreground">Command Model:</span>
                <span className="font-mono text-right text-foreground">
                  {voiceConfig.intelligence.model}
                </span>
              </div>
            </div>
          </div>

          {/* STT Configuration */}
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <Mic className="h-4 w-4" />
              <Label className="text-base font-semibold">Speech-to-Text (STT)</Label>
            </div>

            <div className="grid gap-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="stt-provider">Provider</Label>
                  <Select value={voiceConfig.stt.provider} onValueChange={handleSTTProviderChange}>
                    <SelectTrigger id="stt-provider">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="openai">OpenAI API (Cloud)</SelectItem>
                      <SelectItem value="custom">OpenAI-compatible Endpoint</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="stt-model">Model</Label>
                  <Input
                    id="stt-model"
                    value={voiceConfig.stt.model}
                    onChange={e => handleSTTChange({ model: e.target.value })}
                    placeholder="whisper-1"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="stt-api-key">API Key (Optional)</Label>
                <Input
                  id="stt-api-key"
                  type="password"
                  value={voiceConfig.stt.api_key || ''}
                  onChange={e => handleSTTChange({ api_key: e.target.value })}
                  placeholder="Uses OPENAI_API_KEY env var if not set"
                />
                <p className="text-xs text-muted-foreground">
                  Leave empty to use the OPENAI_API_KEY environment variable
                </p>
              </div>

              {showHostField && (
                <div className="space-y-2">
                  <Label htmlFor="stt-host">Host URL</Label>
                  <Input
                    id="stt-host"
                    value={voiceConfig.stt.host || ''}
                    onChange={e => handleSTTChange({ host: e.target.value })}
                    placeholder="http://localhost:8080"
                  />
                  <p className="text-xs text-muted-foreground">
                    Base URL of your STT service. Do not include <code>/v1</code>; MindRoom appends
                    <code> /v1/audio/transcriptions</code> automatically.
                  </p>
                </div>
              )}

              {missingHostForCustomProvider && (
                <Alert variant="destructive">
                  <Info className="h-4 w-4" />
                  <AlertDescription>
                    OpenAI-compatible endpoint is selected, but the Host URL is empty.
                  </AlertDescription>
                </Alert>
              )}
            </div>
          </div>

          {/* Command Intelligence Model */}
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <Settings className="h-4 w-4" />
              <Label className="text-base font-semibold">Command Intelligence</Label>
            </div>

            <div className="space-y-2">
              <Label htmlFor="intelligence-model">AI Model for Processing</Label>
              <Select
                value={voiceConfig.intelligence.model}
                onValueChange={value => handleIntelligenceChange({ model: value })}
              >
                <SelectTrigger id="intelligence-model">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {availableModels.length > 0 ? (
                    availableModels.map(model => (
                      <SelectItem key={model} value={model}>
                        {model}
                      </SelectItem>
                    ))
                  ) : (
                    <SelectItem value="default">Default Model</SelectItem>
                  )}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Model used to process transcriptions into commands and agent mentions
              </p>
            </div>
          </div>

          {/* Save Button */}
          <div className="flex justify-end">
            <Button onClick={handleSave}>Save Voice Configuration</Button>
          </div>
        </CardContent>
      </Card>

      {/* Voice Features Card */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Info className="h-4 w-4" />
            Voice Features
          </CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="space-y-2 text-sm">
            <li className="flex items-start gap-2">
              <span className="text-primary mt-0.5">🎤</span>
              <span>Automatic transcription of voice messages from all Matrix clients</span>
            </li>
            <li className="flex items-start gap-2">
              <span className="text-primary mt-0.5">🤖</span>
              <span>
                {'Smart command recognition (e.g., "schedule a meeting" -> "!schedule meeting")'}
              </span>
            </li>
            <li className="flex items-start gap-2">
              <span className="text-primary mt-0.5">👥</span>
              <span>{'Agent name detection (e.g., "ask research" -> "@research")'}</span>
            </li>
            <li className="flex items-start gap-2">
              <span className="text-primary mt-0.5">🔒</span>
              <span>Support for both cloud and self-hosted STT services</span>
            </li>
            <li className="flex items-start gap-2">
              <span className="text-primary mt-0.5">🌍</span>
              <span>Multi-language support (depends on STT provider)</span>
            </li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
