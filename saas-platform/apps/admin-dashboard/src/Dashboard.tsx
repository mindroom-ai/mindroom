import { Card, CardContent, CardHeader, Grid, Typography, Box } from '@mui/material'
import { useEffect, useState } from 'react'
import { Title } from 'react-admin'
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell
} from 'recharts'
import { MetricCard } from './components/MetricCard'
import { InstanceHealth } from './components/InstanceHealth'
import { QuickActions } from './components/QuickActions'
import { config } from './config'
import { format } from 'date-fns'

interface DashboardMetrics {
  totalAccounts: number
  activeSubscriptions: number
  runningInstances: number
  mrr: number
  dailyMessages: Array<{ date: string; messages_sent: number }>
  instanceStatuses: Array<{ status: string; count: number }>
  recentActivity: Array<{ time: string; action: string; user: string }>
}

const COLORS = ['#f97316', '#10b981', '#3b82f6', '#ef4444', '#8b5cf6']

export const Dashboard = () => {
  const [metrics, setMetrics] = useState<DashboardMetrics>({
    totalAccounts: 0,
    activeSubscriptions: 0,
    runningInstances: 0,
    mrr: 0,
    dailyMessages: [],
    instanceStatuses: [],
    recentActivity: [],
  })
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchDashboardMetrics()
  }, [])

  const fetchDashboardMetrics = async () => {
    try {
      // Fetch dashboard metrics from backend API
      const response = await fetch(`${config.apiUrl}/metrics/dashboard`)

      if (!response.ok) {
        throw new Error(`Failed to fetch dashboard metrics: ${response.statusText}`)
      }

      const data = await response.json()

      // Format the data for display
      const dailyMessages = data.dailyMessages?.map((item: any) => ({
        date: format(new Date(item.date), 'MMM dd'),
        messages_sent: item.messages_sent
      })) || []

      const recentActivity = data.recentActivity?.map((log: any) => ({
        time: format(new Date(log.created_at), 'HH:mm'),
        action: log.action,
        user: log.account_id?.substring(0, 8) || 'System',
      })) || []

      setMetrics({
        totalAccounts: data.totalAccounts || 0,
        activeSubscriptions: data.activeSubscriptions || 0,
        runningInstances: data.runningInstances || 0,
        mrr: data.mrr || 0,
        dailyMessages,
        instanceStatuses: data.instanceStatuses || [],
        recentActivity,
      })
    } catch (error) {
      console.error('Error fetching dashboard metrics:', error)
    } finally {
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" height="100vh">
        <Typography>Loading dashboard...</Typography>
      </Box>
    )
  }

  return (
    <>
      <Title title="Dashboard" />

      {/* Metric Cards */}
      <Grid container spacing={3} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <MetricCard
            title="Total Accounts"
            value={metrics.totalAccounts}
            icon="👤"
            change="+12%"
            positive
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <MetricCard
            title="Active Subscriptions"
            value={metrics.activeSubscriptions}
            icon="💳"
            change="+8%"
            positive
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <MetricCard
            title="Running Instances"
            value={metrics.runningInstances}
            icon="🖥️"
            change="0%"
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <MetricCard
            title="MRR"
            value={`$${metrics.mrr.toLocaleString()}`}
            icon="💰"
            change="+15%"
            positive
          />
        </Grid>
      </Grid>

      {/* Charts */}
      <Grid container spacing={3} sx={{ mb: 3 }}>
        {/* Daily Messages Chart */}
        <Grid item xs={12} lg={8}>
          <Card>
            <CardHeader
              title={<Typography variant="h6">Daily Messages (Last 7 Days)</Typography>}
            />
            <CardContent>
              <ResponsiveContainer width="100%" height={300}>
                <AreaChart data={metrics.dailyMessages}>
                  <defs>
                    <linearGradient id="colorMessages" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#f97316" stopOpacity={0.8}/>
                      <stop offset="95%" stopColor="#f97316" stopOpacity={0.1}/>
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="date" />
                  <YAxis />
                  <Tooltip />
                  <Area
                    type="monotone"
                    dataKey="messages_sent"
                    stroke="#f97316"
                    fillOpacity={1}
                    fill="url(#colorMessages)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </Grid>

        {/* Instance Status Distribution */}
        <Grid item xs={12} lg={4}>
          <Card>
            <CardHeader
              title={<Typography variant="h6">Instance Status</Typography>}
            />
            <CardContent>
              <ResponsiveContainer width="100%" height={300}>
                <PieChart>
                  <Pie
                    data={metrics.instanceStatuses}
                    cx="50%"
                    cy="50%"
                    labelLine={false}
                    label={({ status, count }) => `${status}: ${count}`}
                    outerRadius={80}
                    fill="#8884d8"
                    dataKey="count"
                  >
                    {metrics.instanceStatuses.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip />
                </PieChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      {/* Instance Health Monitor */}
      <Grid container spacing={3} sx={{ mb: 3 }}>
        <Grid item xs={12}>
          <InstanceHealth />
        </Grid>
      </Grid>

      {/* Quick Actions */}
      <Grid container spacing={3} sx={{ mb: 3 }}>
        <Grid item xs={12}>
          <QuickActions />
        </Grid>
      </Grid>

      {/* Recent Activity */}
      <Grid container spacing={3}>
        <Grid item xs={12}>
          <Card>
            <CardHeader
              title={<Typography variant="h6">Recent Activity</Typography>}
            />
            <CardContent>
              <Box sx={{ maxHeight: 300, overflow: 'auto' }}>
                {metrics.recentActivity.map((activity, index) => (
                  <Box
                    key={index}
                    sx={{
                      py: 1,
                      px: 2,
                      borderBottom: '1px solid',
                      borderColor: 'divider',
                      '&:last-child': { borderBottom: 0 }
                    }}
                  >
                    <Typography variant="body2" color="text.secondary">
                      {activity.time}
                    </Typography>
                    <Typography variant="body1">
                      {activity.action}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      User: {activity.user}
                    </Typography>
                  </Box>
                ))}
              </Box>
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </>
  )
}
