import { LucideIcon } from 'lucide-react';
import { sharedStyles } from './styles';

export interface EmptyStateProps {
  /**
   * Icon to display in the empty state
   */
  icon: LucideIcon;
  /**
   * Primary message to display
   */
  title: string;
  /**
   * Secondary message to display (optional)
   */
  subtitle?: string;
  /**
   * Additional CSS classes
   */
  className?: string;
}

/**
 * Consistent empty state display component for when lists or sections have no content
 */
export function EmptyState({ icon: Icon, title, subtitle, className = '' }: EmptyStateProps) {
  return (
    <div className={`${sharedStyles.empty.container} ${className}`}>
      <Icon className={sharedStyles.empty.icon} />
      <p className={sharedStyles.empty.title}>{title}</p>
      {subtitle && <p className={sharedStyles.empty.subtitle}>{subtitle}</p>}
    </div>
  );
}
