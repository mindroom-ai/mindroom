import { Card, CardContent, CardHeader, Typography, Box, Chip, LinearProgress } from '@mui/material'
import { useEffect, useState } from 'react'
import { createClient } from '@supabase/supabase-js'
import { config } from '../config'
import { CheckCircle, AlertCircle, XCircle, Clock } from 'lucide-react'

interface InstanceHealthData {
  id: string
  dokku_app_name: string
  subdomain: string
  status: string
  health_status: string | null
  last_health_check: string | null
  cpu_limit: string | null
  memory_limit_mb: number | null
}

export const InstanceHealth = () => {
  const [instances, setInstances] = useState<InstanceHealthData[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchInstanceHealth()
    const interval = setInterval(fetchInstanceHealth, 30000) // Refresh every 30 seconds
    return () => clearInterval(interval)
  }, [])

  const fetchInstanceHealth = async () => {
    const supabase = createClient(config.supabaseUrl, config.supabaseServiceKey)

    const { data, error } = await supabase
      .from('instances')
      .select('id, dokku_app_name, subdomain, status, health_status, last_health_check, cpu_limit, memory_limit_mb')
      .order('created_at', { ascending: false })
      .limit(10)

    if (!error && data) {
      setInstances(data as InstanceHealthData[])
    }
    setLoading(false)
  }

  const getHealthIcon = (status: string) => {
    switch (status) {
      case 'running':
        return <CheckCircle className="w-5 h-5 text-green-500" />
      case 'stopped':
        return <Clock className="w-5 h-5 text-yellow-500" />
      case 'failed':
        return <XCircle className="w-5 h-5 text-red-500" />
      default:
        return <AlertCircle className="w-5 h-5 text-gray-500" />
    }
  }

  const getHealthColor = (status: string) => {
    switch (status) {
      case 'running':
        return 'success'
      case 'stopped':
        return 'warning'
      case 'failed':
        return 'error'
      default:
        return 'default'
    }
  }

  if (loading) {
    return <LinearProgress />
  }

  return (
    <Card>
      <CardHeader
        title={<Typography variant="h6">Instance Health Monitor</Typography>}
        action={
          <Typography variant="caption" color="text.secondary">
            Auto-refreshes every 30s
          </Typography>
        }
      />
      <CardContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {instances.map((instance) => (
            <Box
              key={instance.id}
              sx={{
                p: 2,
                border: 1,
                borderColor: 'divider',
                borderRadius: 1,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                '&:hover': {
                  backgroundColor: 'action.hover',
                },
              }}
            >
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                {getHealthIcon(instance.status)}
                <Box>
                  <Typography variant="subtitle1" fontWeight="medium">
                    {instance.dokku_app_name}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {instance.subdomain}
                  </Typography>
                </Box>
              </Box>

              <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                {instance.memory_limit_mb && (
                  <Typography variant="caption" color="text.secondary">
                    Memory: {instance.memory_limit_mb}MB
                  </Typography>
                )}
                {instance.cpu_limit && (
                  <Typography variant="caption" color="text.secondary">
                    CPU: {instance.cpu_limit}
                  </Typography>
                )}
                <Chip
                  label={instance.status.toUpperCase()}
                  color={getHealthColor(instance.status) as any}
                  size="small"
                />
              </Box>
            </Box>
          ))}
        </Box>
      </CardContent>
    </Card>
  )
}
