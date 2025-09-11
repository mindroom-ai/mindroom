'use client'

import { useUsage } from '@/hooks/useUsage'
import { useSubscription } from '@/hooks/useSubscription'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { Loader2, TrendingUp, MessageSquare, Bot, HardDrive } from 'lucide-react'

export default function UsagePage() {
  const { usage, loading: usageLoading } = useUsage(30) // Last 30 days
  const { subscription } = useSubscription()

  if (usageLoading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 className="w-8 h-8 animate-spin text-orange-500" />
      </div>
    )
  }

  const messagesPercentage = subscription && usage
    ? Math.round((usage.totalMessages / (subscription.max_messages_per_day * 30)) * 100)
    : 0

  const storagePercentage = subscription && usage
    ? Math.round((usage.totalStorage / subscription.max_storage_gb) * 100)
    : 0

  const chartData = usage?.dailyUsage.map(day => ({
    date: new Date(day.date).toLocaleDateString('en', { month: 'short', day: 'numeric' }),
    messages: day.messages_sent,
    agents: day.agents_used,
  })) || []

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold dark:text-white">Usage Analytics</h1>

      {/* Summary Cards */}
      <div className="grid md:grid-cols-4 gap-6">
        <Card>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">Total Messages</p>
                <p className="text-2xl font-bold dark:text-white">{usage?.totalMessages.toLocaleString() || 0}</p>
                <p className="text-xs text-gray-500 dark:text-gray-400">Last 30 days</p>
              </div>
              <MessageSquare className="w-8 h-8 text-orange-500" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-gray-600">Active Agents</p>
                <p className="text-2xl font-bold">{usage?.totalAgents || 0}</p>
                <p className="text-xs text-gray-500">Currently deployed</p>
              </div>
              <Bot className="w-8 h-8 text-blue-500" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-gray-600">Storage Used</p>
                <p className="text-2xl font-bold">{usage?.totalStorage.toFixed(1) || 0}GB</p>
                <p className="text-xs text-gray-500">of {subscription?.max_storage_gb || 1}GB</p>
              </div>
              <HardDrive className="w-8 h-8 text-green-500" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-gray-600">Usage Trend</p>
                <p className="text-2xl font-bold">
                  {messagesPercentage > 100 ? '+' : ''}{messagesPercentage}%
                </p>
                <p className="text-xs text-gray-500">vs. limit</p>
              </div>
              <TrendingUp className="w-8 h-8 text-purple-500" />
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Message Usage Chart */}
      <Card>
        <CardHeader>
          <CardTitle>Message Usage Over Time</CardTitle>
        </CardHeader>
        <CardContent>
          {chartData.length > 0 ? (
            <div className="h-80">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 12 }}
                    stroke="#9ca3af"
                  />
                  <YAxis
                    tick={{ fontSize: 12 }}
                    stroke="#9ca3af"
                  />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#fff',
                      border: '1px solid #e5e7eb',
                      borderRadius: '8px',
                    }}
                  />
                  <Legend />
                  <Line
                    type="monotone"
                    dataKey="messages"
                    stroke="#f97316"
                    strokeWidth={2}
                    dot={{ fill: '#f97316', r: 3 }}
                    activeDot={{ r: 5 }}
                    name="Messages Sent"
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="h-80 flex items-center justify-center">
              <p className="text-gray-500">No usage data available yet</p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Agent Usage Chart */}
      <Card>
        <CardHeader>
          <CardTitle>Agent Usage</CardTitle>
        </CardHeader>
        <CardContent>
          {chartData.length > 0 ? (
            <div className="h-80">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 12 }}
                    stroke="#9ca3af"
                  />
                  <YAxis
                    tick={{ fontSize: 12 }}
                    stroke="#9ca3af"
                  />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#fff',
                      border: '1px solid #e5e7eb',
                      borderRadius: '8px',
                    }}
                  />
                  <Legend />
                  <Bar
                    dataKey="agents"
                    fill="#3b82f6"
                    name="Active Agents"
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="h-80 flex items-center justify-center">
              <p className="text-gray-500">No agent data available yet</p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Usage Limits */}
      <Card>
        <CardHeader>
          <CardTitle>Usage vs Limits</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="text-gray-600">Messages</span>
                <span className="font-medium">
                  {usage?.totalMessages.toLocaleString() || 0} / {((subscription?.max_messages_per_day || 100) * 30).toLocaleString()}
                </span>
              </div>
              <div className="w-full bg-gray-200 rounded-full h-2">
                <div
                  className={`h-2 rounded-full transition-all ${
                    messagesPercentage > 80 ? 'bg-red-500' :
                    messagesPercentage > 60 ? 'bg-yellow-500' :
                    'bg-green-500'
                  }`}
                  style={{ width: `${Math.min(messagesPercentage, 100)}%` }}
                />
              </div>
            </div>

            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="text-gray-600">Storage</span>
                <span className="font-medium">
                  {usage?.totalStorage.toFixed(2) || 0}GB / {subscription?.max_storage_gb || 1}GB
                </span>
              </div>
              <div className="w-full bg-gray-200 rounded-full h-2">
                <div
                  className={`h-2 rounded-full transition-all ${
                    storagePercentage > 80 ? 'bg-red-500' :
                    storagePercentage > 60 ? 'bg-yellow-500' :
                    'bg-green-500'
                  }`}
                  style={{ width: `${Math.min(storagePercentage, 100)}%` }}
                />
              </div>
            </div>

            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="text-gray-600">AI Agents</span>
                <span className="font-medium">
                  {usage?.totalAgents || 0} / {subscription?.max_agents || 1}
                </span>
              </div>
              <div className="w-full bg-gray-200 rounded-full h-2">
                <div
                  className="h-2 rounded-full transition-all bg-blue-500"
                  style={{
                    width: `${Math.min(((usage?.totalAgents || 0) / (subscription?.max_agents || 1)) * 100, 100)}%`
                  }}
                />
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
