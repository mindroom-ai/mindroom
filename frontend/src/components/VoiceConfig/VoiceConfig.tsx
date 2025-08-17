import { useState, useEffect } from 'react';
import { Mic, Settings, Volume2, Info, ExternalLink } from 'lucide-react';
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
import { useToast } from '@/components/ui/use-toast';
import { useConfigStore } from '@/store/configStore';
import { VoiceConfig as VoiceConfigType } from '@/types/config';

export function VoiceConfig() {
  const { config, saveConfig, markDirty } = useConfigStore();
  const { toast } = useToast();

  // Initialize local state with default values if voice config doesn't exist
  const [voiceConfig, setVoiceConfig] = useState<VoiceConfigType>({
    enabled: false,
    stt: {
      provider: 'openai',
      model: 'whisper-1',
      api_key: '',
      host: '',
    },
    intelligence: {
      model: 'default',
      confidence_threshold: 0.7,
    },
    ...(config?.voice || {}),
  });

  const [showAdvanced, setShowAdvanced] = useState(false);

  // Update local state when config changes
  useEffect(() => {
    if (config?.voice) {
      setVoiceConfig(config.voice);
    }
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

  const handleIntelligenceChange = (updates: Partial<VoiceConfigType['intelligence']>) => {
    handleVoiceConfigChange({
      intelligence: { ...voiceConfig.intelligence, ...updates },
    });
  };

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

  // Get available models from config
  const availableModels = config?.models ? Object.keys(config.models) : [];

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

        {voiceConfig.enabled && (
          <CardContent className="space-y-6">
            {/* Status Alert */}
            <Alert>
              <Info className="h-4 w-4" />
              <AlertDescription>
                Voice messages will be automatically transcribed and processed. The router agent
                handles all voice messages to avoid duplicates.
              </AlertDescription>
            </Alert>

            {/* STT Configuration */}
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <Mic className="h-4 w-4" />
                <Label className="text-base font-semibold">Speech-to-Text (STT)</Label>
              </div>

              <div className="grid gap-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="stt-provider">Provider</Label>
                    <Select
                      value={voiceConfig.stt.provider}
                      onValueChange={value => handleSTTChange({ provider: value })}
                    >
                      <SelectTrigger id="stt-provider">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="openai">OpenAI Whisper (Cloud)</SelectItem>
                        <SelectItem value="custom">Self-hosted (OpenAI-compatible)</SelectItem>
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

                {voiceConfig.stt.provider === 'openai' && (
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
                )}

                {voiceConfig.stt.provider === 'custom' && (
                  <div className="space-y-2">
                    <Label htmlFor="stt-host">Host URL</Label>
                    <Input
                      id="stt-host"
                      value={voiceConfig.stt.host || ''}
                      onChange={e => handleSTTChange({ host: e.target.value })}
                      placeholder="http://localhost:8080/v1"
                    />
                    <p className="text-xs text-muted-foreground">
                      URL of your self-hosted STT service (OpenAI-compatible API)
                    </p>
                  </div>
                )}
              </div>
            </div>

            {/* Intelligence Configuration */}
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Settings className="h-4 w-4" />
                  <Label className="text-base font-semibold">Command Intelligence</Label>
                </div>
                <Button variant="ghost" size="sm" onClick={() => setShowAdvanced(!showAdvanced)}>
                  {showAdvanced ? 'Hide' : 'Show'} Advanced
                </Button>
              </div>

              {showAdvanced && (
                <div className="grid gap-4 pl-6">
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

                  <div className="space-y-2">
                    <div className="flex justify-between">
                      <Label htmlFor="confidence-threshold">Confidence Threshold</Label>
                      <span className="text-sm text-muted-foreground">
                        {voiceConfig.intelligence.confidence_threshold.toFixed(2)}
                      </span>
                    </div>
                    <input
                      id="confidence-threshold"
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={voiceConfig.intelligence.confidence_threshold}
                      onChange={e =>
                        handleIntelligenceChange({
                          confidence_threshold: parseFloat(e.target.value),
                        })
                      }
                      className="w-full"
                    />
                    <p className="text-xs text-muted-foreground">
                      Higher values require more confidence for command recognition
                    </p>
                  </div>
                </div>
              )}
            </div>

            {/* Save Button */}
            <div className="flex justify-end">
              <Button onClick={handleSave}>Save Voice Configuration</Button>
            </div>
          </CardContent>
        )}
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
                Smart command recognition (e.g., "schedule a meeting" → "!schedule meeting")
              </span>
            </li>
            <li className="flex items-start gap-2">
              <span className="text-primary mt-0.5">👥</span>
              <span>Agent name detection (e.g., "ask research" → "@research")</span>
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

          <div className="mt-4 pt-4 border-t">
            <Button variant="outline" size="sm" asChild>
              <a
                href="/docs/VOICE_MESSAGES.md"
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2"
              >
                <ExternalLink className="h-3 w-3" />
                View Documentation
              </a>
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
