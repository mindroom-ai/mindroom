import { useRecordContext } from 'react-admin'
import { Chip } from '@mui/material'

const statusColors: Record<string, 'success' | 'warning' | 'error' | 'info' | 'default'> = {
  // Account statuses
  active: 'success',
  suspended: 'warning',
  deleted: 'error',
  pending_verification: 'default',

  // Subscription statuses
  trialing: 'info',
  trial: 'info',
  past_due: 'warning',
  cancelled: 'error',
  expired: 'error',
  incomplete: 'warning',
  pending: 'warning',

  // Instance statuses
  provisioning: 'info',
  running: 'success',
  stopped: 'warning',
  failed: 'error',
  deprovisioning: 'default',
}

export const StatusChip = ({ source }: { source: string }) => {
  const record = useRecordContext()
  if (!record) return null

  const status = record[source] as string
  const color = statusColors[status] || 'default'

  return (
    <Chip
      label={status?.replace(/_/g, ' ').toUpperCase()}
      color={color}
      size="small"
    />
  )
}
