import { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { useToast } from '@/components/ui/use-toast';
import { API_BASE } from '@/lib/api';

interface IMDbConfigDialogProps {
  open?: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

export function IMDbConfigDialog({ open = true, onClose, onSuccess }: IMDbConfigDialogProps) {
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(false);
  const { toast } = useToast();

  const handleConfigure = async () => {
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

      localStorage.setItem('imdb_configured', 'true');

      toast({
        title: 'Success!',
        description: 'IMDb has been configured. Agents can now search for movies and TV shows.',
      });

      setApiKey('');
      onSuccess?.();
      onClose();
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

  return (
    <Dialog open={open} onOpenChange={open => !open && onClose()}>
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
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleConfigure} disabled={!apiKey || loading}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Configure'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
