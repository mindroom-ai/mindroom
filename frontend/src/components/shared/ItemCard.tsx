import React, { ReactNode } from "react";
import { Check } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { sharedStyles, getSelectionStyles } from "./styles";

export interface ItemCardBadge {
  /**
   * Badge content
   */
  content: ReactNode;
  /**
   * Badge variant
   */
  variant?: "secondary" | "outline" | "default";
  /**
   * Icon for the badge (will be rendered before content)
   */
  icon?: React.ComponentType<{ className?: string }>;
}

export interface ItemCardProps {
  leading?: ReactNode;
  /**
   * Unique identifier for the item
   */
  id: string;
  /**
   * Item title
   */
  title: string;
  /**
   * Item description (optional)
   */
  description?: string;
  /**
   * Whether this item is currently selected
   */
  isSelected?: boolean;
  /**
   * Click handler
   */
  onClick?: (id: string) => void;
  /**
   * Array of badges to display
   */
  badges?: ItemCardBadge[];
  /**
   * Additional content to render in the card
   */
  children?: ReactNode;
  /**
   * Additional CSS classes
   */
  className?: string;
  /**
   * Whether the item is clickable
   */
  clickable?: boolean;
}

/**
 * Standardized item display card for list items with consistent selection states
 */
export function ItemCard({
  id,
  leading,
  title,
  description,
  isSelected = false,
  onClick,
  badges = [],
  children,
  className = "",
  clickable = true,
}: ItemCardProps) {
  const handleClick = () => {
    if (clickable && onClick) {
      onClick(id);
    }
  };

  return (
    <div
      className={cn(
        "w-full",
        clickable && sharedStyles.item.containerCard,
        getSelectionStyles(isSelected, "card"),
        className,
      )}
      onClick={handleClick}
      role={clickable ? "button" : undefined}
      aria-pressed={clickable ? isSelected : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                handleClick();
              }
            }
          : undefined
      }
    >
      <div className={sharedStyles.item.cardContent}>
        <div className="flex items-start justify-between gap-3">
          {leading}
          <div className="min-w-0 flex-1">
            <h3
              className={cn(
                sharedStyles.item.cardTitle,
                "flex items-start gap-2",
              )}
            >
              <span className="min-w-0 flex-1">{title}</span>
              {isSelected && (
                <Check
                  aria-hidden="true"
                  className="mt-0.5 h-4 w-4 shrink-0 text-primary"
                />
              )}
            </h3>
            {description && (
              <p className={sharedStyles.item.cardDescription}>{description}</p>
            )}
            {badges.length > 0 && (
              <div className={sharedStyles.item.badgeContainer}>
                {badges.map((badge, index) => (
                  <Badge
                    key={index}
                    variant={badge.variant || "secondary"}
                    className={cn(
                      "text-xs",
                      badge.variant === "secondary"
                        ? sharedStyles.badge.secondary
                        : badge.variant === "outline"
                          ? sharedStyles.badge.outline
                          : "text-xs",
                    )}
                  >
                    {badge.icon && (
                      <badge.icon className={sharedStyles.badge.withIcon} />
                    )}
                    {badge.content}
                  </Badge>
                ))}
              </div>
            )}
            {children}
          </div>
        </div>
      </div>
    </div>
  );
}
