import { cn } from "@/lib/utils";
import { ReactNode } from "react";
import { Filter } from "lucide-react";

interface FilterOption {
  value: string;
  label: string | ReactNode;
  count?: number;
  showIcon?: boolean;
  icon?: ReactNode;
}

interface FilterSelectorProps {
  options: FilterOption[];
  value: string | string[];
  onChange: (value: string | string[]) => void;
  multiple?: boolean;
  className?: string;
  size?: "sm" | "md" | "lg";
  showFilterIcon?: boolean;
}

export function FilterSelector({
  options,
  value,
  onChange,
  multiple = false,
  className,
  size = "md",
  showFilterIcon = false,
}: FilterSelectorProps) {
  const sizeClasses = {
    sm: "px-2.5 py-1 text-xs",
    md: "px-3 py-1.5 text-sm",
    lg: "px-4 py-2 text-base",
  };

  const handleClick = (optionValue: string) => {
    if (multiple) {
      const currentValues = Array.isArray(value) ? value : [value];
      const newValues = currentValues.includes(optionValue)
        ? currentValues.filter((v) => v !== optionValue)
        : [...currentValues, optionValue];
      onChange(newValues);
    } else {
      onChange(optionValue);
    }
  };

  const isSelected = (optionValue: string) => {
    if (multiple) {
      return Array.isArray(value)
        ? value.includes(optionValue)
        : value === optionValue;
    }
    return value === optionValue;
  };

  return (
    <div
      className={cn(
        "inline-flex gap-1 rounded-lg border border-border bg-secondary p-1",
        className,
      )}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={isSelected(option.value)}
          onClick={() => handleClick(option.value)}
          className={cn(
            "relative rounded-md font-medium transition-colors duration-150 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring",
            sizeClasses[size],
            isSelected(option.value)
              ? "bg-card text-foreground shadow-sm"
              : "text-muted-foreground hover:bg-card/60 hover:text-foreground",
          )}
        >
          <span className="flex items-center gap-1.5">
            {option.icon
              ? option.icon
              : (showFilterIcon || option.showIcon) && (
                  <Filter
                    className={cn(
                      "w-3.5 h-3.5",
                      isSelected(option.value) && "text-primary",
                    )}
                  />
                )}
            {option.label}
            {option.count !== undefined && (
              <span
                className={cn(
                  "inline-flex items-center justify-center",
                  "min-w-[1.25rem] h-5 px-1 rounded-full",
                  "text-xs font-medium",
                  isSelected(option.value)
                    ? "bg-accent text-accent-foreground"
                    : "bg-foreground/[0.07] text-muted-foreground",
                )}
              >
                {option.count}
              </span>
            )}
          </span>
        </button>
      ))}
    </div>
  );
}
