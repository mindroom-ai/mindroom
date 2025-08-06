import React from 'react';
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
      className: 'text-green-600',
      iconClassName: 'text-green-600',
    },
    syncing: {
      icon: RefreshCw,
      text: 'Syncing...',
      className: 'text-blue-600',
      iconClassName: 'text-blue-600 animate-spin',
    },
    error: {
      icon: AlertCircle,
      text: 'Sync Error',
      className: 'text-red-600',
      iconClassName: 'text-red-600',
    },
    disconnected: {
      icon: WifiOff,
      text: 'Disconnected',
      className: 'text-gray-500',
      iconClassName: 'text-gray-500',
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
