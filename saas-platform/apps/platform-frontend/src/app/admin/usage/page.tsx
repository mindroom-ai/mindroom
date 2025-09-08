import { createServerClientSupabase } from '@/lib/supabase/server'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

export default async function UsagePage() {
  const supabase = await createServerClientSupabase()

  // Get usage metrics for the last 30 days
  const thirtyDaysAgo = new Date()
  thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30)

  const { data: metrics, error } = await supabase
    .from('usage_metrics')
    .select('*, accounts(email, full_name)')
    .gte('created_at', thirtyDaysAgo.toISOString())
    .order('created_at', { ascending: false })

  if (error) {
    console.error('Error fetching usage metrics:', error)
  }

  // Calculate summary statistics
  const totalMessages = metrics?.reduce((sum, m) => sum + (m.message_count || 0), 0) || 0
  const totalStorage = metrics?.reduce((sum, m) => sum + (m.storage_mb || 0), 0) || 0
  const uniqueUsers = new Set(metrics?.map(m => m.account_id)).size

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900">Usage Metrics</h1>
        <p className="text-gray-600 mt-2">Platform usage statistics and trends</p>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Total Messages (30d)</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{totalMessages.toLocaleString()}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Storage Used</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{(totalStorage / 1024).toFixed(2)} GB</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Active Users (30d)</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{uniqueUsers}</div>
          </CardContent>
        </Card>
      </div>

      {/* Detailed Usage Table */}
      <Card>
        <CardHeader>
          <CardTitle>Detailed Usage</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-3 px-4">Date</th>
                  <th className="text-left py-3 px-4">Customer</th>
                  <th className="text-left py-3 px-4">Messages</th>
                  <th className="text-left py-3 px-4">Storage (MB)</th>
                  <th className="text-left py-3 px-4">API Calls</th>
                  <th className="text-left py-3 px-4">Agents</th>
                </tr>
              </thead>
              <tbody>
                {metrics?.map((metric) => (
                  <tr key={metric.id} className="border-b hover:bg-gray-50">
                    <td className="py-3 px-4 text-sm">
                      {new Date(metric.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4">
                      <div>
                        <div className="text-sm font-medium">
                          {metric.accounts?.email}
                        </div>
                        <div className="text-xs text-gray-500">
                          {metric.accounts?.full_name}
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4 text-sm">
                      {metric.message_count || 0}
                    </td>
                    <td className="py-3 px-4 text-sm">
                      {metric.storage_mb || 0}
                    </td>
                    <td className="py-3 px-4 text-sm">
                      {metric.api_calls || 0}
                    </td>
                    <td className="py-3 px-4 text-sm">
                      {metric.agent_count || 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {(!metrics || metrics.length === 0) && (
              <div className="text-center py-8 text-gray-500">
                No usage data available
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
