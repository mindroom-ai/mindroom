import { createServerClientSupabase } from '@/lib/supabase/server'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Users, CreditCard, Server, Activity } from 'lucide-react'

export default async function AdminDashboard() {
  const supabase = await createServerClientSupabase()

  // Fetch statistics
  const [
    { count: accountCount },
    { count: subscriptionCount },
    { count: instanceCount },
  ] = await Promise.all([
    supabase.from('accounts').select('*', { count: 'exact', head: true }),
    supabase.from('subscriptions').select('*', { count: 'exact', head: true }),
    supabase.from('instances').select('*', { count: 'exact', head: true }),
  ])

  const stats = [
    {
      title: 'Total Accounts',
      value: accountCount || 0,
      icon: Users,
      change: '+12%',
      changeType: 'positive' as const
    },
    {
      title: 'Active Subscriptions',
      value: subscriptionCount || 0,
      icon: CreditCard,
      change: '+8%',
      changeType: 'positive' as const
    },
    {
      title: 'Running Instances',
      value: instanceCount || 0,
      icon: Server,
      change: '+23%',
      changeType: 'positive' as const
    },
    {
      title: 'System Health',
      value: '99.9%',
      icon: Activity,
      change: 'Operational',
      changeType: 'neutral' as const
    },
  ]

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900">Admin Dashboard</h1>
        <p className="text-gray-600 mt-2">System overview and key metrics</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        {stats.map((stat) => (
          <Card key={stat.title}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">
                {stat.title}
              </CardTitle>
              <stat.icon className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{stat.value}</div>
              <p className={`text-xs ${
                stat.changeType === 'positive' ? 'text-green-600' :
                stat.changeType === 'negative' ? 'text-red-600' :
                'text-gray-600'
              }`}>
                {stat.change}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Recent Activity */}
      <Card>
        <CardHeader>
          <CardTitle>Recent Activity</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">New account registered</p>
                <p className="text-xs text-gray-500">user@example.com - 2 minutes ago</p>
              </div>
            </div>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Instance deployed</p>
                <p className="text-xs text-gray-500">customer-123 - 15 minutes ago</p>
              </div>
            </div>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Subscription upgraded</p>
                <p className="text-xs text-gray-500">Pro plan - 1 hour ago</p>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
