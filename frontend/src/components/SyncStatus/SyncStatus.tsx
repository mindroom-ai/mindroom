import { Check, RefreshCw, AlertCircle, WifiOff } from 'lucide-react';
import { cn } from '@/lib/utils';

interface SyncStatusProps {
  status: 'synced' | 'syncing' | 'error' | 'disconnected';
}

export function SyncStatus({ status }: SyncStatusProps) {
  const statusConfig = {
    synced: {
      icon: Check,
      text: 'Synced',
      className: 'text-green-300',
      iconClassName: 'text-green-300',
    },
    syncing: {
      icon: RefreshCw,
      text: 'Syncing...',
      className: 'text-blue-300',
      iconClassName: 'text-blue-300 animate-spin',
    },
    error: {
      icon: AlertCircle,
      text: 'Sync Error',
      className: 'text-red-300',
      iconClassName: 'text-red-300',
    },
    disconnected: {
      icon: WifiOff,
      text: 'Disconnected',
      className: 'text-gray-300',
      iconClassName: 'text-gray-300',
    },
  };

  const config = statusConfig[status];
  const Icon = config.icon;

  return (
    <div className={cn('flex items-center gap-2 text-sm', config.className)}>
      <Icon className={cn('h-4 w-4', config.iconClassName)} />
      <span>{config.text}</span>
    </div>
  );
}
