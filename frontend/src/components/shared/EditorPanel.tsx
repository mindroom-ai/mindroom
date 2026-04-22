import React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toaster";
import type { SaveConfigResult } from "@/store/configStore";
import { Save, Trash2, LucideIcon, ArrowLeft } from "lucide-react";

const DEFAULT_STALE_SAVE_MESSAGE = "Save was superseded by newer draft edits.";

function isSaveConfigResult(value: unknown): value is SaveConfigResult {
  if (typeof value !== "object" || value === null || !("status" in value)) {
    return false;
  }
  return (
    value.status === "saved" ||
    value.status === "stale" ||
    value.status === "error"
  );
}

export function showSaveFailureToastIfNeeded(
  result: unknown,
  options: {
    title?: string;
    staleMessage?: string;
    fallbackMessage?: string;
  } = {},
): boolean {
  if (!isSaveConfigResult(result) || result.status === "saved") {
    return false;
  }

  const description =
    result.status === "stale"
      ? (options.staleMessage ?? DEFAULT_STALE_SAVE_MESSAGE)
      : (result.diagnostics.find((diagnostic) => diagnostic.kind === "global")
          ?.message ??
        result.message ??
        options.fallbackMessage ??
        "Failed to save changes.");

  toast({
    title: options.title ?? "Save Failed",
    description,
    variant: "destructive",
  });
  return true;
}

export interface EditorPanelProps {
  /** Icon to display in the header */
  icon: LucideIcon;
  /** Title for the panel header */
  title: string;
  /** Whether the panel is in a dirty state (has unsaved changes) */
  isDirty: boolean;
  /** Function to call when save is clicked */
  onSave: () => void | Promise<unknown>;
  /** Function to call when delete is clicked */
  onDelete: () => void;
  /** Whether to show the save and delete buttons */
  showActions?: boolean;
  /** Whether the save button should be disabled */
  disableSave?: boolean;
  /** Whether the delete button should be disabled */
  disableDelete?: boolean;
  /** Content to render in the panel */
  children: React.ReactNode;
  /** Custom class name for the panel */
  className?: string;
  /** Function to call when back button is clicked (mobile only) */
  onBack?: () => void;
}

export interface EditorPanelEmptyStateProps {
  /** Icon to display in the empty state */
  icon: LucideIcon;
  /** Message to display when nothing is selected */
  message: string;
  /** Custom class name for the empty state */
  className?: string;
}

/**
 * Empty state component for when no item is selected
 */
export function EditorPanelEmptyState({
  icon: Icon,
  message,
  className = "",
}: EditorPanelEmptyStateProps) {
  return (
    <Card className={`h-full flex items-center justify-center ${className}`}>
      <div className="text-gray-500 dark:text-gray-400 text-center">
        <Icon className="h-12 w-12 mx-auto mb-2 text-gray-300" />
        <p>{message}</p>
      </div>
    </Card>
  );
}

/**
 * Standardized editor panel component with consistent header, actions, and layout
 */
export function EditorPanel({
  icon: Icon,
  title,
  isDirty,
  onSave,
  onDelete,
  showActions = true,
  disableSave = false,
  disableDelete = false,
  children,
  className = "",
  onBack,
}: EditorPanelProps) {
  const handleSave = async () => {
    try {
      const result = await onSave();
      showSaveFailureToastIfNeeded(result);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to save changes.";
      toast({
        title: "Save Failed",
        description: message,
        variant: "destructive",
      });
      console.error("Save failed:", error);
    }
  };

  const handleDelete = () => {
    onDelete();
  };

  return (
    <Card className={`h-full flex flex-col overflow-hidden ${className}`}>
      <CardHeader className="pb-3 flex-shrink-0">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            {onBack && (
              <Button
                variant="ghost"
                size="sm"
                onClick={onBack}
                className="lg:hidden -ml-2 mr-1"
              >
                <ArrowLeft className="h-4 w-4" />
              </Button>
            )}
            <Icon className="h-5 w-5" />
            {title}
          </CardTitle>
          {showActions && (
            <div className="flex gap-2">
              <Button
                variant="destructive"
                size="sm"
                onClick={handleDelete}
                disabled={disableDelete}
              >
                <Trash2 className="h-4 w-4 sm:mr-1" />
                <span className="hidden sm:inline">Delete</span>
              </Button>
              <Button
                variant="default"
                size="sm"
                onClick={handleSave}
                disabled={!isDirty || disableSave}
              >
                <Save className="h-4 w-4 sm:mr-1" />
                <span className="hidden sm:inline">Save</span>
              </Button>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex-1 overflow-y-auto min-h-0">
        <div className="space-y-4">{children}</div>
      </CardContent>
    </Card>
  );
}
