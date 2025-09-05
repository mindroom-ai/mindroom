import { Card, CardContent, Typography, Box } from '@mui/material'
import { TrendingUp, TrendingDown, Minus } from 'lucide-react'

interface MetricCardProps {
  title: string
  value: string | number
  icon: string
  change?: string
  positive?: boolean
}

export const MetricCard = ({ title, value, icon, change, positive }: MetricCardProps) => {
  const getTrendIcon = () => {
    if (!change) return null
    if (change === '0%') return <Minus className="w-4 h-4" />
    if (positive) return <TrendingUp className="w-4 h-4" />
    return <TrendingDown className="w-4 h-4" />
  }

  const getTrendColor = () => {
    if (!change || change === '0%') return 'text.secondary'
    return positive ? 'success.main' : 'error.main'
  }

  return (
    <Card>
      <CardContent>
        <Box display="flex" justifyContent="space-between" alignItems="start">
          <Box>
            <Typography color="text.secondary" gutterBottom variant="body2">
              {title}
            </Typography>
            <Typography variant="h4" component="div" sx={{ mb: 1 }}>
              {value}
            </Typography>
            {change && (
              <Box display="flex" alignItems="center" gap={0.5}>
                {getTrendIcon()}
                <Typography variant="body2" color={getTrendColor()}>
                  {change}
                </Typography>
              </Box>
            )}
          </Box>
          <Typography variant="h4">{icon}</Typography>
        </Box>
      </CardContent>
    </Card>
  )
}
