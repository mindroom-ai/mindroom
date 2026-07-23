import { Settings } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import type { ToolInfo } from "@/hooks/useTools";
import { ToolInfoPopover } from "./ToolInfoPopover";

type ToolSectionKind = "configured" | "default" | "setupRequired";

interface ToolSelectionRowProps {
  tool: ToolInfo;
  sectionKind: ToolSectionKind;
  checkboxId: string;
  isChecked: boolean;
  isActive: boolean;
  hasOverrides: boolean;
  showSettings: boolean;
  setupBlocked?: boolean;
  onCheckedChange: (checked: boolean) => void;
  onToggleSettings: () => void;
}

export function ToolSelectionRow({
  tool,
  sectionKind,
  checkboxId,
  isChecked,
  isActive,
  hasOverrides,
  showSettings,
  setupBlocked = false,
  onCheckedChange,
  onToggleSettings,
}: ToolSelectionRowProps) {
  const activeClass =
    isActive && showSettings
      ? "bg-blue-50 dark:bg-blue-500/10"
      : "hover:bg-gray-50 dark:hover:bg-white/5";

  return (
    <div className={`rounded-lg p-2 transition-colors ${activeClass}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-start space-x-3 sm:space-x-2">
          <Checkbox
            id={checkboxId}
            checked={isChecked}
            aria-label={
              isChecked && showSettings ? tool.display_name : undefined
            }
            onCheckedChange={(checked) => onCheckedChange(checked === true)}
            className="mt-0.5 h-5 w-5 flex-shrink-0 sm:h-4 sm:w-4"
          />
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              {isChecked && showSettings ? (
                <button
                  type="button"
                  onClick={onToggleSettings}
                  className="min-w-0 truncate text-left text-sm font-medium leading-none"
                >
                  {tool.display_name}
                </button>
              ) : (
                <label
                  htmlFor={checkboxId}
                  className="min-w-0 truncate text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer select-none"
                >
                  {tool.display_name}
                </label>
              )}
              {hasOverrides && (
                <Badge
                  variant="secondary"
                  className="flex-shrink-0 text-[10px] uppercase tracking-wide"
                >
                  Customized
                </Badge>
              )}
            </div>
            {tool.description && (
              <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                {tool.description}
              </p>
            )}
            {setupBlocked && (
              <p className="mt-1 text-xs text-muted-foreground">
                This scope can use runtime env credentials, but dashboard
                credential setup is only supported for shared deployment
                credentials.
              </p>
            )}
          </div>
        </div>
        <div className="flex flex-shrink-0 items-center gap-2">
          <ToolInfoPopover tool={tool} />
          {sectionKind === "setupRequired" && (
            <Badge variant="secondary" className="text-xs">
              Setup required
            </Badge>
          )}
          {isChecked && showSettings && (
            <Button
              type="button"
              variant={isActive ? "secondary" : "ghost"}
              size="sm"
              onClick={onToggleSettings}
              className="h-8 px-2"
            >
              <Settings className="h-4 w-4 sm:mr-1" />
              <span className="hidden sm:inline">
                {hasOverrides ? "Edit" : "Settings"}
              </span>
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
