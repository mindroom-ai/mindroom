'use client'

import { useUsage } from '@/hooks/useUsage'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { Loader2 } from 'lucide-react'
import type { Subscription } from '@/hooks/useSubscription'

export function UsageChart({ subscription }: { subscription: Subscription | null }) {
  const { usage, loading } = useUsage(7) // Last 7 days

  if (loading) {
    return (
      <div className="h-64 flex items-center justify-center">
        <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
      </div>
    )
  }

  if (!usage || usage.dailyUsage.length === 0) {
    return (
      <div className="h-64 flex items-center justify-center">
        <p className="text-gray-500">No usage data available yet</p>
      </div>
    )
  }

  // Format data for the chart
  const chartData = usage.dailyUsage.map(day => ({
    date: new Date(day.date).toLocaleDateString('en', { month: 'short', day: 'numeric' }),
    messages: day.messages_sent,
    limit: subscription?.max_messages_per_day || 100,
  }))

  // Calculate usage percentage
  const usagePercentage = subscription
    ? Math.round((usage.totalMessages / (subscription.max_messages_per_day * 7)) * 100)
    : 0

  return (
    <div>
      {/* Summary Stats */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div>
          <p className="text-sm text-gray-600">Messages Sent</p>
          <p className="text-2xl font-bold">{usage.totalMessages.toLocaleString()}</p>
          <p className="text-xs text-gray-500">Last 7 days</p>
        </div>
        <div>
          <p className="text-sm text-gray-600">Active Agents</p>
          <p className="text-2xl font-bold">{usage.totalAgents}</p>
          <p className="text-xs text-gray-500">Currently deployed</p>
        </div>
        <div>
          <p className="text-sm text-gray-600">Storage Used</p>
          <p className="text-2xl font-bold">{usage.totalStorage.toFixed(1)}GB</p>
          <p className="text-xs text-gray-500">
            of {subscription?.max_storage_gb || 1}GB
          </p>
        </div>
      </div>

      {/* Usage Bar */}
      <div className="mb-6">
        <div className="flex justify-between text-sm mb-1">
          <span className="text-gray-600">Daily Message Limit</span>
          <span className="font-medium">{usagePercentage}% used</span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-2">
          <div
            className={`h-2 rounded-full transition-all ${
              usagePercentage > 80 ? 'bg-red-500' :
              usagePercentage > 60 ? 'bg-yellow-500' :
              'bg-green-500'
            }`}
            style={{ width: `${Math.min(usagePercentage, 100)}%` }}
          />
        </div>
      </div>

      {/* Chart */}
      <div className="h-64">
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
              dot={{ fill: '#f97316', r: 4 }}
              activeDot={{ r: 6 }}
              name="Messages"
            />
            <Line
              type="monotone"
              dataKey="limit"
              stroke="#e5e7eb"
              strokeWidth={1}
              strokeDasharray="5 5"
              dot={false}
              name="Daily Limit"
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
