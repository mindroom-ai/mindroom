import { useState, useEffect } from 'react';
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

interface ConfigField {
  name: string;
  label: string;
  type: string;
  required?: boolean;
  default?: any;
  placeholder?: string;
  description?: string;
  validation?: {
    min?: number;
    max?: number;
  };
}

interface GenericConfigDialogProps {
  open: boolean;
  onClose: () => void;
  service: string;
  displayName: string;
  description: string;
  configFields: ConfigField[];
  onSuccess?: () => void;
  isEditing?: boolean;
}

export function GenericConfigDialog({
  open,
  onClose,
  service,
  displayName,
  description,
  configFields,
  onSuccess,
  isEditing = false,
}: GenericConfigDialogProps) {
  const [configValues, setConfigValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [loadingExisting, setLoadingExisting] = useState(false);
  const { toast } = useToast();

  // Initialize default values and load existing credentials
  useEffect(() => {
    if (!open) return;

    const loadExistingCredentials = async () => {
      setLoadingExisting(true);
      try {
        // Try to load existing credentials
        const response = await fetch(`${API_BASE}/api/credentials/${service}`);
        if (response.ok) {
          const data = await response.json();
          if (data.credentials) {
            // Merge existing credentials with defaults
            const defaults: Record<string, string> = {};
            configFields.forEach(field => {
              if (data.credentials[field.name] !== undefined) {
                defaults[field.name] = String(data.credentials[field.name]);
              } else if (field.default !== undefined && field.default !== null) {
                defaults[field.name] = String(field.default);
              }
            });
            setConfigValues(defaults);
            return;
          }
        }
      } catch (error) {
        console.log('No existing credentials found');
      } finally {
        setLoadingExisting(false);
      }

      // If no existing credentials, just use defaults
      const defaults: Record<string, string> = {};
      configFields.forEach(field => {
        if (field.default !== undefined && field.default !== null) {
          defaults[field.name] = String(field.default);
        }
      });
      setConfigValues(defaults);
    };

    loadExistingCredentials();
  }, [configFields, service, open]);

  const handleSave = async () => {
    // Validate all required fields are filled
    const missingFields = configFields.filter(field => field.required && !configValues[field.name]);
    if (missingFields.length > 0) {
      toast({
        title: 'Missing Configuration',
        description: `Please fill in: ${missingFields.map(f => f.label).join(', ')}`,
        variant: 'destructive',
      });
      return;
    }

    // Validate field constraints
    for (const field of configFields) {
      const value = configValues[field.name];
      if (value && field.validation) {
        if (field.type === 'number') {
          const numValue = Number(value);
          if (field.validation.min !== undefined && numValue < field.validation.min) {
            toast({
              title: 'Invalid Value',
              description: `${field.label} must be at least ${field.validation.min}`,
              variant: 'destructive',
            });
            return;
          }
          if (field.validation.max !== undefined && numValue > field.validation.max) {
            toast({
              title: 'Invalid Value',
              description: `${field.label} must be at most ${field.validation.max}`,
              variant: 'destructive',
            });
            return;
          }
        }
      }
    }

    setLoading(true);
    try {
      // Save all config values as environment variables using our credentials API
      const response = await fetch(`${API_BASE}/api/credentials/${service}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          credentials: configValues,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to save configuration`);
      }

      toast({
        title: 'Success!',
        description: `${displayName} has been configured successfully.`,
      });

      onSuccess?.();
      onClose();
    } catch (error) {
      toast({
        title: 'Configuration Failed',
        description: error instanceof Error ? error.message : 'Failed to save configuration',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>
            {isEditing ? 'Edit' : 'Configure'} {displayName}
          </DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {loadingExisting ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <>
            <div className="grid gap-4 py-4">
              {configFields.map(field => (
                <div key={field.name} className="grid gap-2">
                  <Label htmlFor={field.name}>
                    {field.label}
                    {field.required && <span className="text-destructive ml-1">*</span>}
                  </Label>
                  <Input
                    id={field.name}
                    type={field.type || 'text'}
                    placeholder={field.placeholder}
                    value={configValues[field.name] || ''}
                    onChange={e =>
                      setConfigValues({ ...configValues, [field.name]: e.target.value })
                    }
                    min={field.validation?.min}
                    max={field.validation?.max}
                  />
                  {field.description && (
                    <p className="text-sm text-muted-foreground">{field.description}</p>
                  )}
                </div>
              ))}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={onClose} disabled={loading}>
                Cancel
              </Button>
              <Button onClick={handleSave} disabled={loading}>
                {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {isEditing ? 'Update' : 'Save'} Configuration
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
