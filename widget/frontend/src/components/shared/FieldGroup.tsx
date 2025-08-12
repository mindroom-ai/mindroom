import React from 'react';
import { Label } from '@/components/ui/label';

export interface FieldGroupProps {
  /** The label text for the field */
  label: string;
  /** Optional helper text to explain the field */
  helperText?: string;
  /** Whether the field is required */
  required?: boolean;
  /** Error message to display */
  error?: string;
  /** ID for the field (used for label htmlFor) */
  htmlFor?: string;
  /** Custom actions or buttons for the field (e.g., Add button for arrays) */
  actions?: React.ReactNode;
  /** The form field component */
  children: React.ReactNode;
  /** Custom class name for the field group */
  className?: string;
}

/**
 * Standardized form field wrapper with consistent label, helper text, and error styling
 */
export function FieldGroup({
  label,
  helperText,
  required = false,
  error,
  htmlFor,
  actions,
  children,
  className = '',
}: FieldGroupProps) {
  return (
    <div className={`space-y-2 ${className}`}>
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
        <div className="flex-1">
          <Label htmlFor={htmlFor} className="text-sm font-medium">
            {label}
            {required && <span className="text-destructive ml-1">*</span>}
          </Label>
          {helperText && <p className="text-xs text-muted-foreground mt-1">{helperText}</p>}
        </div>
        {actions && <div className="flex items-center gap-1">{actions}</div>}
      </div>

      <div className="space-y-1">
        {children}
        {error && <p className="text-xs text-destructive mt-1">{error}</p>}
      </div>
    </div>
  );
}
