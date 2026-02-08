import { Check, RefreshCw, AlertCircle, WifiOff } from 'lucide-react';
import { cn } from '@/lib/utils';

const STATUS_CONFIG = {
  synced: {
    icon: Check,
    text: 'Synced',
    className: 'text-green-300',
    iconClassName: 'text-green-300',
    dotClassName: 'bg-green-400',
  },
  syncing: {
    icon: RefreshCw,
    text: 'Syncing...',
    className: 'text-blue-300',
    iconClassName: 'text-blue-300 animate-spin',
    dotClassName: 'bg-blue-400 animate-pulse',
  },
  error: {
    icon: AlertCircle,
    text: 'Sync Error',
    className: 'text-red-300',
    iconClassName: 'text-red-300',
    dotClassName: 'bg-red-400',
  },
  disconnected: {
    icon: WifiOff,
    text: 'Disconnected',
    className: 'text-gray-300',
    iconClassName: 'text-gray-300',
    dotClassName: 'bg-gray-400',
  },
} as const;

interface SyncStatusProps {
  status: 'synced' | 'syncing' | 'error' | 'disconnected';
  compact?: boolean;
  className?: string;
}

export function SyncStatus({ status, compact = false, className }: SyncStatusProps) {
  const config = STATUS_CONFIG[status];
  const Icon = config.icon;

  if (compact) {
    return (
      <div
        className={cn('flex h-9 w-9 items-center justify-center', className)}
        aria-label={config.text}
        role="status"
      >
        <span className={cn('h-2.5 w-2.5 rounded-full', config.dotClassName)} />
        <span className="sr-only">{config.text}</span>
      </div>
    );
  }

  return (
    <div className={cn('flex items-center gap-2 text-sm', config.className, className)}>
      <Icon className={cn('h-4 w-4', config.iconClassName)} />
      <span>{config.text}</span>
    </div>
  );
}
