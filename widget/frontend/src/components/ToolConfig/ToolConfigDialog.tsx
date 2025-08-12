import { useState, useEffect } from 'react';
import { useConfigStore } from '@/store/configStore';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Settings, AlertCircle } from 'lucide-react';
import { TOOL_SCHEMAS, ToolConfigValues, getDefaultToolConfig } from '@/types/toolConfig';
import { useToast } from '@/components/ui/use-toast';

interface ToolConfigDialogProps {
  toolId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ToolConfigDialog({ toolId, open, onOpenChange }: ToolConfigDialogProps) {
  const { config, updateToolConfig } = useConfigStore();
  const { toast } = useToast();
  const [values, setValues] = useState<ToolConfigValues>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const schema = TOOL_SCHEMAS[toolId];

  useEffect(() => {
    if (open && config) {
      // Load existing config or defaults
      const existingConfig = config.tools?.[toolId] || {};
      const defaultConfig = getDefaultToolConfig(toolId);
      setValues({ ...defaultConfig, ...existingConfig });
      setErrors({});
    }
  }, [open, toolId, config]);

  if (!schema) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Tool Configuration</DialogTitle>
            <DialogDescription>This tool does not require configuration.</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>
    );
  }

  const handleFieldChange = (fieldName: string, value: any) => {
    setValues(prev => ({ ...prev, [fieldName]: value }));
    // Clear error for this field
    setErrors(prev => {
      const newErrors = { ...prev };
      delete newErrors[fieldName];
      return newErrors;
    });
  };

  const validateForm = (): boolean => {
    const newErrors: Record<string, string> = {};

    schema.fields.forEach(field => {
      const value = values[field.name];

      // Check required fields
      if (field.required && (!value || value === '')) {
        newErrors[field.name] = `${field.label} is required`;
        return;
      }

      // Type-specific validation
      if (value !== undefined && value !== '' && field.validation) {
        if (field.type === 'number') {
          const numValue = Number(value);
          if (field.validation.min !== undefined && numValue < field.validation.min) {
            newErrors[field.name] = `Must be at least ${field.validation.min}`;
          }
          if (field.validation.max !== undefined && numValue > field.validation.max) {
            newErrors[field.name] = `Must be at most ${field.validation.max}`;
          }
        }
        if (field.validation.pattern) {
          const regex = new RegExp(field.validation.pattern);
          if (!regex.test(value)) {
            newErrors[field.name] = field.validation.message || 'Invalid format';
          }
        }
      }
    });

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSave = () => {
    if (!validateForm()) {
      toast({
        title: 'Validation Error',
        description: 'Please fix the errors before saving',
        variant: 'destructive',
      });
      return;
    }

    updateToolConfig(toolId, values);
    toast({
      title: 'Configuration Saved',
      description: `${schema.name} configuration has been updated`,
    });
    onOpenChange(false);
  };

  const renderField = (field: (typeof schema.fields)[0]) => {
    const value = values[field.name] ?? '';
    const error = errors[field.name];

    switch (field.type) {
      case 'text':
      case 'url':
        return (
          <div key={field.name} className="space-y-2">
            <Label htmlFor={field.name}>{field.label}</Label>
            <Input
              id={field.name}
              type={field.type === 'url' ? 'url' : 'text'}
              value={value}
              onChange={e => handleFieldChange(field.name, e.target.value)}
              placeholder={field.placeholder}
              className={error ? 'border-red-500' : ''}
            />
            {field.description && <p className="text-xs text-gray-500">{field.description}</p>}
            {error && (
              <p className="text-xs text-red-500 flex items-center gap-1">
                <AlertCircle className="h-3 w-3" />
                {error}
              </p>
            )}
          </div>
        );

      case 'password':
        return (
          <div key={field.name} className="space-y-2">
            <Label htmlFor={field.name}>{field.label}</Label>
            <Input
              id={field.name}
              type="password"
              value={value}
              onChange={e => handleFieldChange(field.name, e.target.value)}
              placeholder={field.placeholder}
              className={error ? 'border-red-500' : ''}
            />
            {field.description && <p className="text-xs text-gray-500">{field.description}</p>}
            {error && (
              <p className="text-xs text-red-500 flex items-center gap-1">
                <AlertCircle className="h-3 w-3" />
                {error}
              </p>
            )}
          </div>
        );

      case 'number':
        return (
          <div key={field.name} className="space-y-2">
            <Label htmlFor={field.name}>{field.label}</Label>
            <Input
              id={field.name}
              type="number"
              value={value}
              onChange={e => handleFieldChange(field.name, parseInt(e.target.value) || 0)}
              min={field.validation?.min}
              max={field.validation?.max}
              className={error ? 'border-red-500' : ''}
            />
            {field.description && <p className="text-xs text-gray-500">{field.description}</p>}
            {error && (
              <p className="text-xs text-red-500 flex items-center gap-1">
                <AlertCircle className="h-3 w-3" />
                {error}
              </p>
            )}
          </div>
        );

      case 'boolean':
        return (
          <div key={field.name} className="flex items-center space-x-2">
            <Checkbox
              id={field.name}
              checked={value || false}
              onCheckedChange={checked => handleFieldChange(field.name, checked)}
            />
            <Label htmlFor={field.name} className="cursor-pointer">
              {field.label}
            </Label>
          </div>
        );

      case 'select':
        return (
          <div key={field.name} className="space-y-2">
            <Label htmlFor={field.name}>{field.label}</Label>
            <Select
              value={value || field.default}
              onValueChange={val => handleFieldChange(field.name, val)}
            >
              <SelectTrigger id={field.name} className={error ? 'border-red-500' : ''}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {field.options?.map(option => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {field.description && <p className="text-xs text-gray-500">{field.description}</p>}
            {error && (
              <p className="text-xs text-red-500 flex items-center gap-1">
                <AlertCircle className="h-3 w-3" />
                {error}
              </p>
            )}
          </div>
        );

      default:
        return null;
    }
  };

  // Group fields by category if there are many
  const groupedFields = schema.fields.reduce(
    (acc, field) => {
      const category = field.required ? 'Required' : 'Optional';
      if (!acc[category]) acc[category] = [];
      acc[category].push(field);
      return acc;
    },
    {} as Record<string, typeof schema.fields>
  );

  const hasMultipleCategories = Object.keys(groupedFields).length > 1;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[95vw] max-w-2xl sm:w-full max-h-[85vh] overflow-hidden">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Settings className="h-5 w-5" />
            Configure {schema.name}
          </DialogTitle>
          <DialogDescription>{schema.description}</DialogDescription>
        </DialogHeader>

        <ScrollArea className="max-h-[50vh] pr-2 sm:pr-4">
          {schema.fields.length === 0 ? (
            <div className="text-center py-8 text-gray-500">
              This tool does not require any configuration.
            </div>
          ) : hasMultipleCategories ? (
            <Tabs defaultValue="Required" className="w-full">
              <TabsList className="mb-4">
                {Object.keys(groupedFields).map(category => (
                  <TabsTrigger key={category} value={category}>
                    {category}
                  </TabsTrigger>
                ))}
              </TabsList>
              {Object.entries(groupedFields).map(([category, fields]) => (
                <TabsContent key={category} value={category} className="space-y-4">
                  {fields.map(renderField)}
                </TabsContent>
              ))}
            </Tabs>
          ) : (
            <div className="space-y-4">{schema.fields.map(renderField)}</div>
          )}
        </ScrollArea>

        <DialogFooter className="flex-col sm:flex-row gap-2">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            className="w-full sm:w-auto"
          >
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            disabled={schema.fields.length === 0}
            className="w-full sm:w-auto"
          >
            Save Configuration
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
