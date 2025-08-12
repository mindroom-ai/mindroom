import { useState, useEffect } from 'react';
import { Key, Eye, EyeOff, Check, X, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/components/ui/use-toast';
import { Badge } from '@/components/ui/badge';

interface ApiKeyConfigProps {
  service: string;
  displayName: string;
  description?: string;
  keyName?: string;
  onConfigured?: () => void;
}

export function ApiKeyConfig({
  service,
  displayName,
  description,
  keyName = 'api_key',
  onConfigured,
}: ApiKeyConfigProps) {
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [loading, setLoading] = useState(false);
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [maskedKey, setMaskedKey] = useState<string | null>(null);
  const { toast } = useToast();

  // Check if API key is already configured
  useEffect(() => {
    checkApiKey();
  }, [service]);

  const checkApiKey = async () => {
    try {
      const response = await fetch(`/api/credentials/${service}/api-key?key_name=${keyName}`);
      if (response.ok) {
        const data = await response.json();
        setHasKey(data.has_key);
        setMaskedKey(data.masked_key || null);
      }
    } catch (error) {
      console.error('Failed to check API key:', error);
    }
  };

  const handleSave = async () => {
    if (!apiKey.trim()) {
      toast({
        title: 'Error',
        description: 'Please enter an API key',
        variant: 'destructive',
      });
      return;
    }

    setLoading(true);
    try {
      const response = await fetch(`/api/credentials/${service}/api-key`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service,
          api_key: apiKey,
          key_name: keyName,
        }),
      });

      if (response.ok) {
        toast({
          title: 'Success',
          description: `API key saved for ${displayName}`,
        });
        setApiKey('');
        await checkApiKey();
        onConfigured?.();
      } else {
        throw new Error('Failed to save API key');
      }
    } catch (error) {
      toast({
        title: 'Error',
        description: 'Failed to save API key',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm(`Are you sure you want to delete the API key for ${displayName}?`)) {
      return;
    }

    setLoading(true);
    try {
      const response = await fetch(`/api/credentials/${service}`, {
        method: 'DELETE',
      });

      if (response.ok) {
        toast({
          title: 'Success',
          description: `API key deleted for ${displayName}`,
        });
        await checkApiKey();
        onConfigured?.();
      } else {
        throw new Error('Failed to delete API key');
      }
    } catch (error) {
      toast({
        title: 'Error',
        description: 'Failed to delete API key',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleTest = async () => {
    setLoading(true);
    try {
      const response = await fetch(`/api/credentials/${service}/test`, {
        method: 'POST',
      });

      if (response.ok) {
        toast({
          title: 'Success',
          description: 'API key is valid',
        });
      } else {
        const error = await response.json();
        throw new Error(error.detail || 'Invalid API key');
      }
    } catch (error) {
      toast({
        title: 'Error',
        description: error instanceof Error ? error.message : 'Failed to test API key',
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
          <Key className="h-5 w-5" />
          {displayName} API Configuration
        </CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-2">
          <Badge variant={hasKey ? 'default' : 'secondary'}>
            {hasKey ? <Check className="h-3 w-3 mr-1" /> : <X className="h-3 w-3 mr-1" />}
            {hasKey ? 'Configured' : 'Not Configured'}
          </Badge>
          {hasKey && maskedKey && (
            <span className="text-sm text-muted-foreground font-mono">{maskedKey}</span>
          )}
        </div>

        <div className="space-y-2">
          <Label htmlFor={`api-key-${service}`}>API Key</Label>
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Input
                id={`api-key-${service}`}
                type={showKey ? 'text' : 'password'}
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
                placeholder={hasKey ? 'Enter new API key to replace' : 'Enter API key'}
                disabled={loading}
              />
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="absolute right-0 top-0 h-full px-3 hover:bg-transparent"
                onClick={() => setShowKey(!showKey)}
              >
                {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </Button>
            </div>
          </div>
        </div>

        <div className="flex gap-2">
          <Button onClick={handleSave} disabled={loading || !apiKey.trim()} className="flex-1">
            {loading ? (
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
            ) : (
              <Key className="h-4 w-4 mr-2" />
            )}
            {hasKey ? 'Update' : 'Save'} API Key
          </Button>

          {hasKey && (
            <>
              <Button onClick={handleTest} disabled={loading} variant="outline">
                Test
              </Button>
              <Button onClick={handleDelete} disabled={loading} variant="destructive">
                Delete
              </Button>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
