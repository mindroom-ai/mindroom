import { Card, CardContent, CardHeader, Typography, Button, Box } from '@mui/material'
import {
  RefreshCw,
  AlertTriangle,
  Mail,
  Database,
  Shield,
  Activity
} from 'lucide-react'
import { useNotify } from 'react-admin'
import { useState } from 'react'

export const QuickActions = () => {
  const notify = useNotify()
  const [loading, setLoading] = useState<string | null>(null)

  const handleAction = async (action: string) => {
    setLoading(action)
    try {
      // Simulate API calls - in real implementation, these would call actual endpoints
      await new Promise(resolve => setTimeout(resolve, 1000))

      switch (action) {
        case 'restart-failed':
          notify('Restarting all failed instances...', { type: 'info' })
          break
        case 'backup':
          notify('Database backup initiated', { type: 'success' })
          break
        case 'clear-cache':
          notify('Cache cleared successfully', { type: 'success' })
          break
        case 'health-check':
          notify('Health check complete - all systems operational', { type: 'success' })
          break
        case 'send-alerts':
          notify('Alert digest sent to admin team', { type: 'success' })
          break
        case 'security-scan':
          notify('Security scan initiated', { type: 'info' })
          break
      }
    } catch (error) {
      notify(`Action failed: ${action}`, { type: 'error' })
    } finally {
      setLoading(null)
    }
  }

  const actions = [
    {
      id: 'restart-failed',
      label: 'Restart Failed Instances',
      icon: <RefreshCw className="w-5 h-5" />,
      color: 'warning' as const,
    },
    {
      id: 'backup',
      label: 'Backup Database',
      icon: <Database className="w-5 h-5" />,
      color: 'primary' as const,
    },
    {
      id: 'clear-cache',
      label: 'Clear Cache',
      icon: <AlertTriangle className="w-5 h-5" />,
      color: 'secondary' as const,
    },
    {
      id: 'health-check',
      label: 'Run Health Check',
      icon: <Activity className="w-5 h-5" />,
      color: 'success' as const,
    },
    {
      id: 'send-alerts',
      label: 'Send Alert Digest',
      icon: <Mail className="w-5 h-5" />,
      color: 'info' as const,
    },
    {
      id: 'security-scan',
      label: 'Security Scan',
      icon: <Shield className="w-5 h-5" />,
      color: 'error' as const,
    },
  ]

  return (
    <Card>
      <CardHeader
        title={<Typography variant="h6">Quick Actions</Typography>}
      />
      <CardContent>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 2 }}>
          {actions.map(action => (
            <Button
              key={action.id}
              variant="outlined"
              color={action.color}
              startIcon={action.icon}
              onClick={() => handleAction(action.id)}
              disabled={loading !== null}
              sx={{
                justifyContent: 'flex-start',
                textTransform: 'none',
                py: 1.5,
              }}
            >
              {loading === action.id ? 'Processing...' : action.label}
            </Button>
          ))}
        </Box>
      </CardContent>
    </Card>
  )
}
