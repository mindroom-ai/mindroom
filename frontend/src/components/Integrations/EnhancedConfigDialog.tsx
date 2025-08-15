import { useState, useEffect } from 'react';
import {
  Loader2,
  Info,
  ExternalLink,
  Shield,
  CheckCircle,
  AlertCircle,
  Key,
  Lock,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Separator } from '@/components/ui/separator';
import { Badge } from '@/components/ui/badge';
import { useToast } from '@/components/ui/use-toast';
import { API_BASE } from '@/lib/api';
import { cn } from '@/lib/utils';

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

interface EnhancedConfigDialogProps {
  open: boolean;
  onClose: () => void;
  service: string;
  displayName: string;
  description: string;
  configFields: ConfigField[];
  onSuccess?: () => void;
  isEditing?: boolean;
  docsUrl?: string | null;
  helperText?: string | null;
  icon?: any;
  iconColor?: string;
}

export function EnhancedConfigDialog({
  open,
  onClose,
  service,
  displayName,
  description,
  configFields,
  onSuccess,
  isEditing = false,
  docsUrl,
  helperText,
  icon: Icon,
  iconColor,
}: EnhancedConfigDialogProps) {
  const [configValues, setConfigValues] = useState<Record<string, string | boolean>>({});
  const [loading, setLoading] = useState(false);
  const [loadingExisting, setLoadingExisting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [showPassword, setShowPassword] = useState<Record<string, boolean>>({});
  const { toast } = useToast();

  // Function to render markdown links in helper text
  const renderHelperText = (text: string) => {
    // Simple markdown link parser for [text](url)
    const parts = text.split(/(\[.*?\]\(.*?\))/g);
    return parts.map((part, index) => {
      const linkMatch = part.match(/\[(.*?)\]\((.*?)\)/);
      if (linkMatch) {
        const [, linkText, url] = linkMatch;
        return (
          <a
            key={index}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-500 dark:text-blue-400 underline hover:text-blue-600 dark:hover:text-blue-300"
          >
            {linkText}
          </a>
        );
      }
      return part;
    });
  };

  // Just use the config fields as provided - no special filtering needed
  const filteredFields = configFields;

  // Initialize default values and load existing credentials
  useEffect(() => {
    if (!open) return;

    const loadExistingCredentials = async () => {
      setLoadingExisting(true);

      // Use the config fields directly
      let fieldsToUse = configFields;

      try {
        // Try to load existing credentials
        const response = await fetch(`${API_BASE}/api/credentials/${service}`);
        if (response.ok) {
          const data = await response.json();
          if (data.credentials) {
            // Merge existing credentials with defaults
            const defaults: Record<string, string | boolean> = {};
            fieldsToUse.forEach(field => {
              if (data.credentials[field.name] !== undefined) {
                // Keep boolean values as boolean, convert others to string
                if (field.type === 'boolean') {
                  defaults[field.name] =
                    data.credentials[field.name] === true ||
                    data.credentials[field.name] === 'true';
                } else {
                  defaults[field.name] = String(data.credentials[field.name]);
                }
              } else if (field.default !== undefined && field.default !== null) {
                // Use default value with proper type
                if (field.type === 'boolean') {
                  defaults[field.name] = field.default === true || field.default === 'true';
                } else {
                  defaults[field.name] = String(field.default);
                }
              }
            });
            setConfigValues(defaults);
            setLoadingExisting(false);
            return;
          }
        }
      } catch (error) {
        console.log('No existing credentials found');
      }

      // If no existing credentials, just use defaults
      const defaults: Record<string, string | boolean> = {};
      fieldsToUse.forEach(field => {
        if (field.default !== undefined && field.default !== null) {
          if (field.type === 'boolean') {
            defaults[field.name] = field.default === true || field.default === 'true';
          } else {
            defaults[field.name] = String(field.default);
          }
        }
      });
      setConfigValues(defaults);
      setLoadingExisting(false);
    };

    loadExistingCredentials();
  }, [service, open, configFields]); // Use stable dependencies

  const validateField = (field: ConfigField, value: string | boolean): string | null => {
    // Boolean fields don't need validation
    if (field.type === 'boolean') {
      return null;
    }

    if (field.required && !value) {
      return `${field.label} is required`;
    }

    if (value && field.validation && typeof value === 'string') {
      if (field.type === 'number') {
        const numValue = Number(value);
        if (isNaN(numValue)) {
          return `${field.label} must be a number`;
        }
        if (field.validation.min !== undefined && numValue < field.validation.min) {
          return `${field.label} must be at least ${field.validation.min}`;
        }
        if (field.validation.max !== undefined && numValue > field.validation.max) {
          return `${field.label} must be at most ${field.validation.max}`;
        }
      }
    }

    return null;
  };

  const handleFieldChange = (field: ConfigField, value: string | boolean) => {
    setConfigValues({ ...configValues, [field.name]: value });

    // Clear error when user starts typing or toggling
    if (fieldErrors[field.name]) {
      const newErrors = { ...fieldErrors };
      delete newErrors[field.name];
      setFieldErrors(newErrors);
    }
  };

  const handleSave = async () => {
    // Validate all fields
    const errors: Record<string, string> = {};
    filteredFields.forEach(field => {
      const error = validateField(field, configValues[field.name]);
      if (error) {
        errors[field.name] = error;
      }
    });

    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors);
      toast({
        title: 'Validation Error',
        description: 'Please fix the errors before saving.',
        variant: 'destructive',
      });
      return;
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
        description: `${displayName} has been ${
          isEditing ? 'updated' : 'configured'
        } successfully.`,
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

  const getFieldIcon = (field: ConfigField) => {
    if (
      field.type === 'password' ||
      field.name.toLowerCase().includes('token') ||
      field.name.toLowerCase().includes('key') ||
      field.name.toLowerCase().includes('secret')
    ) {
      return Key;
    }
    return null;
  };

  const togglePasswordVisibility = (fieldName: string) => {
    setShowPassword(prev => ({ ...prev, [fieldName]: !prev[fieldName] }));
  };

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[600px]">
        <DialogHeader className="space-y-3">
          <div className="flex items-center space-x-3">
            {Icon && (
              <div className={cn('p-2 rounded-lg bg-muted', iconColor)}>
                <Icon className="h-5 w-5" />
              </div>
            )}
            <div>
              <DialogTitle className="text-xl">
                {isEditing ? 'Edit' : 'Configure'} {displayName}
              </DialogTitle>
              <DialogDescription className="mt-1">{description}</DialogDescription>
            </div>
          </div>

          {/* Security Notice */}
          <Alert className="border-muted">
            <Shield className="h-4 w-4" />
            <AlertDescription className="text-xs">
              Your credentials are encrypted and stored securely. They are never shared with third
              parties.
            </AlertDescription>
          </Alert>

          {/* Helper Text */}
          {helperText && (
            <Alert className="border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/30">
              <Info className="h-4 w-4 text-blue-600 dark:text-blue-400" />
              <AlertDescription className="text-xs text-blue-900 dark:text-blue-100">
                {renderHelperText(helperText)}
              </AlertDescription>
            </Alert>
          )}

          {/* Documentation Link - More Prominent */}
          {docsUrl && (
            <Button
              variant="outline"
              className="w-full flex items-center justify-center gap-2 bg-gradient-to-r from-orange-50 to-amber-50 dark:from-orange-950/20 dark:to-amber-950/20 hover:from-orange-100 hover:to-amber-100 dark:hover:from-orange-950/30 dark:hover:to-amber-950/30 border-orange-200 dark:border-orange-800"
              onClick={() => window.open(docsUrl, '_blank')}
            >
              <ExternalLink className="h-4 w-4" />
              View Official Documentation
            </Button>
          )}
        </DialogHeader>

        <Separator className="my-4" />

        {loadingExisting ? (
          <div className="flex flex-col items-center justify-center py-12 space-y-3">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            <p className="text-sm text-muted-foreground">Loading configuration...</p>
          </div>
        ) : (
          <>
            <div className="space-y-4 py-2 max-h-[400px] overflow-y-auto px-1">
              {filteredFields.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-8 text-center">
                  <CheckCircle className="h-12 w-12 text-green-500 mb-3" />
                  <p className="text-sm text-muted-foreground">This service is fully configured.</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    No additional configuration is required.
                  </p>
                </div>
              ) : (
                filteredFields.map((field, index) => {
                  const FieldIcon = getFieldIcon(field);
                  const isPasswordField =
                    field.type === 'password' ||
                    field.name.toLowerCase().includes('token') ||
                    field.name.toLowerCase().includes('key') ||
                    field.name.toLowerCase().includes('secret');
                  const hasError = !!fieldErrors[field.name];

                  return (
                    <div key={field.name} className="space-y-2">
                      {index > 0 && <Separator className="my-4" />}

                      {field.type === 'boolean' ? (
                        <div className="flex items-center space-x-3 p-3 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-all duration-200">
                          <Checkbox
                            id={field.name}
                            checked={configValues[field.name] === true}
                            onCheckedChange={checked => handleFieldChange(field, checked === true)}
                            className="h-5 w-5"
                          />
                          <label htmlFor={field.name} className="flex-1 cursor-pointer select-none">
                            <div className="flex items-center space-x-2">
                              {FieldIcon && <FieldIcon className="h-4 w-4 text-muted-foreground" />}
                              <span className="font-medium text-sm">{field.label}</span>
                              {field.required && (
                                <Badge variant="secondary" className="ml-2 text-xs px-1.5 py-0">
                                  Required
                                </Badge>
                              )}
                            </div>
                            {field.description && (
                              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1 ml-6">
                                {field.description}
                              </div>
                            )}
                          </label>
                        </div>
                      ) : (
                        <>
                          <div className="space-y-1">
                            <Label
                              htmlFor={field.name}
                              className={cn(
                                'flex items-center space-x-2',
                                hasError && 'text-destructive'
                              )}
                            >
                              {FieldIcon && <FieldIcon className="h-4 w-4 text-muted-foreground" />}
                              <span>{field.label}</span>
                              {field.required && (
                                <Badge variant="secondary" className="ml-2 text-xs px-1.5 py-0">
                                  Required
                                </Badge>
                              )}
                            </Label>
                            {field.description && (
                              <p className="text-xs text-muted-foreground ml-6">
                                {field.description}
                              </p>
                            )}
                          </div>
                          <div className="relative">
                            <Input
                              id={field.name}
                              type={
                                isPasswordField && !showPassword[field.name]
                                  ? 'password'
                                  : field.type === 'number'
                                    ? 'number'
                                    : 'text'
                              }
                              placeholder={field.placeholder}
                              value={(configValues[field.name] as string) || ''}
                              onChange={e => handleFieldChange(field, e.target.value)}
                              min={field.validation?.min}
                              max={field.validation?.max}
                              className={cn(
                                'pr-10',
                                hasError && 'border-destructive focus-visible:ring-destructive',
                                isPasswordField && 'font-mono'
                              )}
                            />

                            {isPasswordField && (
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                className="absolute right-0 top-0 h-full px-3 hover:bg-transparent"
                                onClick={() => togglePasswordVisibility(field.name)}
                              >
                                {showPassword[field.name] ? (
                                  <Lock className="h-4 w-4 text-muted-foreground" />
                                ) : (
                                  <Lock className="h-4 w-4 text-muted-foreground" />
                                )}
                              </Button>
                            )}
                          </div>

                          {hasError && (
                            <div className="flex items-center space-x-1 text-destructive">
                              <AlertCircle className="h-3 w-3" />
                              <p className="text-xs">{fieldErrors[field.name]}</p>
                            </div>
                          )}

                          {field.validation && (
                            <p className="text-xs text-muted-foreground">
                              {field.type === 'number' &&
                                field.validation.min !== undefined &&
                                field.validation.max !== undefined &&
                                `Value must be between ${field.validation.min} and ${field.validation.max}`}
                            </p>
                          )}
                        </>
                      )}
                    </div>
                  );
                })
              )}
            </div>

            <Separator className="my-4" />

            <DialogFooter className="flex items-center justify-between sm:justify-between">
              <div className="flex items-center space-x-2 text-xs text-muted-foreground">
                {isEditing ? (
                  <>
                    <Info className="h-3 w-3" />
                    <span>Updating will replace existing credentials</span>
                  </>
                ) : (
                  <>
                    <CheckCircle className="h-3 w-3" />
                    <span>Ready to configure</span>
                  </>
                )}
              </div>

              <div className="flex space-x-2">
                <Button variant="outline" onClick={onClose} disabled={loading}>
                  Cancel
                </Button>
                <Button
                  onClick={handleSave}
                  disabled={loading || Object.keys(fieldErrors).length > 0}
                  className="min-w-[120px]"
                >
                  {loading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Saving...
                    </>
                  ) : (
                    <>{isEditing ? 'Update' : 'Save'} Configuration</>
                  )}
                </Button>
              </div>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
