# Agent 5: Admin Dashboard

## Project Context

You are working on MindRoom, an AI agent platform SaaS. The admin dashboard is for internal use to manage customers, instances, and monitor the platform.

### Understanding the System

First, read these files:
1. `README.md` - Product overview
2. `deploy/platform/database/init.sql` - Database schema you'll be working with
3. `frontend/src/App.tsx` - See the existing MindRoom configuration UI for style reference

### The Goal

Build an admin dashboard for MindRoom operations team to:
- View all customers and their subscriptions
- Manually provision/deprovision instances
- Monitor instance health and resource usage
- Handle failed payments and support tickets
- View platform-wide metrics

This will be a React Admin application with Supabase as the data provider.

## Your Specific Task

You will work ONLY in the `apps/admin-dashboard/` directory to build the internal admin panel.

### Step 1: Initialize React Admin

```bash
cd apps
npm create react-admin admin-dashboard
cd admin-dashboard
npm install @supabase/supabase-js ra-supabase recharts
npm install lucide-react date-fns
```

### Step 2: Project Structure

```
apps/admin-dashboard/
├── src/
│   ├── App.tsx                    # Main app with React Admin
│   ├── index.tsx                  # Entry point
│   ├── authProvider.ts           # Supabase auth provider
│   ├── dataProvider.ts           # Supabase data provider
│   ├── Dashboard.tsx             # Main dashboard view
│   ├── resources/
│   │   ├── accounts/
│   │   │   ├── AccountList.tsx
│   │   │   ├── AccountShow.tsx
│   │   │   ├── AccountEdit.tsx
│   │   │   └── index.ts
│   │   ├── subscriptions/
│   │   │   ├── SubscriptionList.tsx
│   │   │   ├── SubscriptionShow.tsx
│   │   │   ├── SubscriptionEdit.tsx
│   │   │   └── index.ts
│   │   ├── instances/
│   │   │   ├── InstanceList.tsx
│   │   │   ├── InstanceShow.tsx
│   │   │   ├── InstanceActions.tsx
│   │   │   └── index.ts
│   │   ├── audit_logs/
│   │   │   ├── AuditLogList.tsx
│   │   │   └── index.ts
│   │   └── metrics/
│   │       ├── MetricsView.tsx
│   │       └── index.ts
│   ├── components/
│   │   ├── StatusChip.tsx        # Colored status indicators
│   │   ├── MetricCard.tsx        # Dashboard metric cards
│   │   ├── UsageChart.tsx        # Usage charts
│   │   ├── InstanceHealth.tsx    # Instance health monitor
│   │   └── QuickActions.tsx      # Common admin actions
│   ├── services/
│   │   ├── provisioner.ts        # Call Dokku provisioner
│   │   ├── stripe.ts             # Stripe operations
│   │   └── monitoring.ts         # Health checks
│   └── config.ts                 # Configuration
├── public/
├── .env.example
├── package.json
├── Dockerfile
└── README.md
```

### Step 3: Core Implementation

#### A. `src/App.tsx` - Main Application
```typescript
import {
  Admin,
  Resource,
  ListGuesser,
  EditGuesser,
  ShowGuesser,
  defaultTheme,
} from 'react-admin'
import { dataProvider } from './dataProvider'
import { authProvider } from './authProvider'
import { Dashboard } from './Dashboard'
import { Layout } from './Layout'

// Resources
import * as accounts from './resources/accounts'
import * as subscriptions from './resources/subscriptions'
import * as instances from './resources/instances'
import * as auditLogs from './resources/audit_logs'

// Icons
import {
  Users,
  CreditCard,
  Server,
  FileText,
  BarChart3,
} from 'lucide-react'

const theme = {
  ...defaultTheme,
  palette: {
    primary: {
      main: '#f97316', // Orange to match MindRoom brand
    },
  },
}

function App() {
  return (
    <Admin
      title="MindRoom Admin"
      dataProvider={dataProvider}
      authProvider={authProvider}
      dashboard={Dashboard}
      theme={theme}
      layout={Layout}
      requireAuth
    >
      <Resource
        name="accounts"
        list={accounts.AccountList}
        show={accounts.AccountShow}
        edit={accounts.AccountEdit}
        icon={Users}
      />
      <Resource
        name="subscriptions"
        list={subscriptions.SubscriptionList}
        show={subscriptions.SubscriptionShow}
        edit={subscriptions.SubscriptionEdit}
        icon={CreditCard}
      />
      <Resource
        name="instances"
        list={instances.InstanceList}
        show={instances.InstanceShow}
        icon={Server}
      />
      <Resource
        name="audit_logs"
        list={auditLogs.AuditLogList}
        icon={FileText}
        options={{ label: 'Audit Logs' }}
      />
      <Resource
        name="usage_metrics"
        list={ListGuesser}
        icon={BarChart3}
        options={{ label: 'Usage Metrics' }}
      />
    </Admin>
  )
}

export default App
```

#### B. `src/dataProvider.ts` - Supabase Data Provider
```typescript
import { DataProvider } from 'react-admin'
import { createClient } from '@supabase/supabase-js'

const supabaseUrl = process.env.REACT_APP_SUPABASE_URL!
const supabaseServiceKey = process.env.REACT_APP_SUPABASE_SERVICE_KEY!

const supabase = createClient(supabaseUrl, supabaseServiceKey)

export const dataProvider: DataProvider = {
  getList: async (resource, params) => {
    const { page, perPage } = params.pagination
    const { field, order } = params.sort
    const { filter } = params

    // Build query
    let query = supabase
      .from(resource)
      .select('*', { count: 'exact' })

    // Apply filters
    Object.keys(filter).forEach(key => {
      if (filter[key] !== undefined && filter[key] !== '') {
        query = query.eq(key, filter[key])
      }
    })

    // Apply sorting
    if (field) {
      query = query.order(field, { ascending: order === 'ASC' })
    }

    // Apply pagination
    const start = (page - 1) * perPage
    const end = start + perPage - 1
    query = query.range(start, end)

    const { data, count, error } = await query

    if (error) throw error

    return {
      data: data || [],
      total: count || 0,
    }
  },

  getOne: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .select('*')
      .eq('id', params.id)
      .single()

    if (error) throw error

    return { data }
  },

  getMany: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .select('*')
      .in('id', params.ids)

    if (error) throw error

    return { data: data || [] }
  },

  getManyReference: async (resource, params) => {
    const { page, perPage } = params.pagination
    const { field, order } = params.sort

    let query = supabase
      .from(resource)
      .select('*', { count: 'exact' })
      .eq(params.target, params.id)

    if (field) {
      query = query.order(field, { ascending: order === 'ASC' })
    }

    const start = (page - 1) * perPage
    const end = start + perPage - 1
    query = query.range(start, end)

    const { data, count, error } = await query

    if (error) throw error

    return {
      data: data || [],
      total: count || 0,
    }
  },

  create: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .insert(params.data)
      .select()
      .single()

    if (error) throw error

    return { data }
  },

  update: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .update(params.data)
      .eq('id', params.id)
      .select()
      .single()

    if (error) throw error

    return { data }
  },

  updateMany: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .update(params.data)
      .in('id', params.ids)
      .select()

    if (error) throw error

    return { data: params.ids }
  },

  delete: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .delete()
      .eq('id', params.id)
      .select()
      .single()

    if (error) throw error

    return { data }
  },

  deleteMany: async (resource, params) => {
    const { error } = await supabase
      .from(resource)
      .delete()
      .in('id', params.ids)

    if (error) throw error

    return { data: params.ids }
  },
}
```

#### C. `src/Dashboard.tsx` - Main Dashboard
```tsx
import { Card, CardContent, CardHeader, CardTitle } from '@mui/material'
import { useEffect, useState } from 'react'
import { Title } from 'react-admin'
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer
} from 'recharts'
import { MetricCard } from './components/MetricCard'
import { InstanceHealth } from './components/InstanceHealth'
import { createClient } from '@supabase/supabase-js'

export const Dashboard = () => {
  const [metrics, setMetrics] = useState({
    totalAccounts: 0,
    activeSubscriptions: 0,
    runningInstances: 0,
    mrr: 0,
    dailyMessages: [],
    instanceStatuses: [],
  })

  useEffect(() => {
    fetchDashboardMetrics()
  }, [])

  const fetchDashboardMetrics = async () => {
    const supabase = createClient(
      process.env.REACT_APP_SUPABASE_URL!,
      process.env.REACT_APP_SUPABASE_SERVICE_KEY!
    )

    // Get counts
    const [accounts, subscriptions, instances] = await Promise.all([
      supabase.from('accounts').select('*', { count: 'exact', head: true }),
      supabase.from('subscriptions').select('*', { count: 'exact', head: true }).eq('status', 'active'),
      supabase.from('instances').select('*', { count: 'exact', head: true }).eq('status', 'running'),
    ])

    // Get MRR (Monthly Recurring Revenue)
    const { data: mrrData } = await supabase
      .from('subscriptions')
      .select('tier')
      .eq('status', 'active')

    const tierPrices = {
      starter: 49,
      professional: 199,
      enterprise: 999,
    }

    const mrr = mrrData?.reduce((sum, sub) => {
      return sum + (tierPrices[sub.tier] || 0)
    }, 0) || 0

    // Get daily message metrics for last 7 days
    const { data: messageData } = await supabase
      .from('usage_metrics')
      .select('date, messages_sent')
      .gte('date', new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString())
      .order('date')

    // Get instance status distribution
    const { data: instanceData } = await supabase
      .from('instances')
      .select('status')

    const statusCounts = instanceData?.reduce((acc, inst) => {
      acc[inst.status] = (acc[inst.status] || 0) + 1
      return acc
    }, {})

    setMetrics({
      totalAccounts: accounts.count || 0,
      activeSubscriptions: subscriptions.count || 0,
      runningInstances: instances.count || 0,
      mrr,
      dailyMessages: messageData || [],
      instanceStatuses: Object.entries(statusCounts || {}).map(([status, count]) => ({
        status,
        count,
      })),
    })
  }

  return (
    <>
      <Title title="Dashboard" />

      {/* Metric Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <MetricCard
          title="Total Accounts"
          value={metrics.totalAccounts}
          icon="👤"
          change="+12%"
          positive
        />
        <MetricCard
          title="Active Subscriptions"
          value={metrics.activeSubscriptions}
          icon="💳"
          change="+8%"
          positive
        />
        <MetricCard
          title="Running Instances"
          value={metrics.runningInstances}
          icon="🖥️"
          change="0%"
        />
        <MetricCard
          title="MRR"
          value={`$${metrics.mrr.toLocaleString()}`}
          icon="💰"
          change="+15%"
          positive
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* Daily Messages Chart */}
        <Card>
          <CardHeader>
            <CardTitle>Daily Messages (Last 7 Days)</CardTitle>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={300}>
              <AreaChart data={metrics.dailyMessages}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" />
                <YAxis />
                <Tooltip />
                <Area
                  type="monotone"
                  dataKey="messages_sent"
                  stroke="#f97316"
                  fill="#fed7aa"
                />
              </AreaChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        {/* Instance Status Distribution */}
        <Card>
          <CardHeader>
            <CardTitle>Instance Status Distribution</CardTitle>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={metrics.instanceStatuses}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="status" />
                <YAxis />
                <Tooltip />
                <Bar dataKey="count" fill="#f97316" />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      </div>

      {/* Instance Health Monitor */}
      <InstanceHealth />
    </>
  )
}
```

#### D. `src/resources/instances/InstanceList.tsx` - Instance Management
```tsx
import {
  List,
  Datagrid,
  TextField,
  DateField,
  EditButton,
  ShowButton,
  BulkDeleteButton,
  FilterButton,
  SearchInput,
  SelectInput,
  TopToolbar,
  CreateButton,
  ExportButton,
  useListContext,
  useNotify,
  useRefresh,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'
import { Button } from '@mui/material'
import { Play, Pause, RotateCw, Trash2 } from 'lucide-react'

const InstanceActions = ({ record }) => {
  const notify = useNotify()
  const refresh = useRefresh()

  const handleAction = async (action: string) => {
    try {
      const response = await fetch('/api/instances/' + action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ app_name: record.dokku_app_name }),
      })

      if (response.ok) {
        notify(`Instance ${action} successful`, { type: 'success' })
        refresh()
      } else {
        throw new Error('Action failed')
      }
    } catch (error) {
      notify(`Failed to ${action} instance`, { type: 'error' })
    }
  }

  return (
    <div className="flex gap-2">
      {record.status === 'running' ? (
        <Button
          size="small"
          onClick={() => handleAction('stop')}
          startIcon={<Pause className="w-4 h-4" />}
        >
          Stop
        </Button>
      ) : (
        <Button
          size="small"
          onClick={() => handleAction('start')}
          startIcon={<Play className="w-4 h-4" />}
        >
          Start
        </Button>
      )}
      <Button
        size="small"
        onClick={() => handleAction('restart')}
        startIcon={<RotateCw className="w-4 h-4" />}
      >
        Restart
      </Button>
      <ShowButton />
    </div>
  )
}

const instanceFilters = [
  <SearchInput source="q" alwaysOn />,
  <SelectInput
    source="status"
    choices={[
      { id: 'provisioning', name: 'Provisioning' },
      { id: 'running', name: 'Running' },
      { id: 'stopped', name: 'Stopped' },
      { id: 'failed', name: 'Failed' },
    ]}
  />,
]

const ListActions = () => (
  <TopToolbar>
    <FilterButton />
    <CreateButton label="Provision Instance" />
    <ExportButton />
  </TopToolbar>
)

export const InstanceList = () => (
  <List
    filters={instanceFilters}
    actions={<ListActions />}
    sort={{ field: 'created_at', order: 'DESC' }}
  >
    <Datagrid rowClick={false}>
      <TextField source="dokku_app_name" label="App Name" />
      <TextField source="subdomain" />
      <StatusChip source="status" />
      <TextField source="memory_limit_mb" label="Memory (MB)" />
      <TextField source="cpu_limit" label="CPU Cores" />
      <DateField source="created_at" showTime />
      <InstanceActions />
    </Datagrid>
  </List>
)
```

#### E. `src/resources/accounts/AccountShow.tsx` - Account Details
```tsx
import {
  Show,
  SimpleShowLayout,
  TextField,
  EmailField,
  DateField,
  BooleanField,
  ReferenceManyField,
  Datagrid,
  EditButton,
  TabbedShowLayout,
  Tab,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'
import { Card, CardContent } from '@mui/material'

export const AccountShow = () => (
  <Show>
    <TabbedShowLayout>
      <Tab label="Overview">
        <TextField source="id" />
        <EmailField source="email" />
        <TextField source="full_name" />
        <TextField source="company_name" />
        <StatusChip source="status" />
        <BooleanField source="email_verified" />
        <DateField source="created_at" showTime />
        <DateField source="last_login" showTime />
      </Tab>

      <Tab label="Subscription">
        <ReferenceManyField
          label="Subscriptions"
          reference="subscriptions"
          target="account_id"
        >
          <Datagrid>
            <TextField source="tier" />
            <StatusChip source="status" />
            <TextField source="max_agents" />
            <TextField source="max_messages_per_day" />
            <DateField source="current_period_end" />
            <EditButton />
          </Datagrid>
        </ReferenceManyField>
      </Tab>

      <Tab label="Instances">
        <ReferenceManyField
          label="Instances"
          reference="instances"
          target="subscription_id"
        >
          <Datagrid>
            <TextField source="dokku_app_name" />
            <TextField source="subdomain" />
            <StatusChip source="status" />
            <DateField source="provisioned_at" showTime />
            <EditButton />
          </Datagrid>
        </ReferenceManyField>
      </Tab>

      <Tab label="Usage">
        <ReferenceManyField
          label="Recent Usage"
          reference="usage_metrics"
          target="instance_id"
          sort={{ field: 'date', order: 'DESC' }}
        >
          <Datagrid>
            <DateField source="date" />
            <TextField source="messages_sent" />
            <TextField source="agents_used" />
          </Datagrid>
        </ReferenceManyField>
      </Tab>

      <Tab label="Audit Log">
        <ReferenceManyField
          label="Recent Activity"
          reference="audit_logs"
          target="account_id"
          sort={{ field: 'created_at', order: 'DESC' }}
        >
          <Datagrid>
            <DateField source="created_at" showTime />
            <TextField source="action" />
            <TextField source="ip_address" />
          </Datagrid>
        </ReferenceManyField>
      </Tab>
    </TabbedShowLayout>
  </Show>
)
```

#### F. `src/components/StatusChip.tsx` - Status Indicator
```tsx
import { useRecordContext } from 'react-admin'
import { Chip } from '@mui/material'

const statusColors = {
  // Account statuses
  active: 'success',
  suspended: 'warning',
  deleted: 'error',
  pending_verification: 'default',

  // Subscription statuses
  trialing: 'info',
  past_due: 'warning',
  cancelled: 'error',
  incomplete: 'warning',

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

  const status = record[source]
  const color = statusColors[status] || 'default'

  return (
    <Chip
      label={status?.replace('_', ' ').toUpperCase()}
      color={color as any}
      size="small"
    />
  )
}
```

### Step 4: Admin-Specific Features

#### A. Manual Instance Provisioning
Add ability to manually provision instances for customers who need special setup.

#### B. Impersonation
Allow admins to view the customer portal as a specific user for support purposes.

#### C. Bulk Operations
- Bulk email customers
- Bulk subscription updates
- Bulk instance restarts

#### D. Monitoring Dashboard
Real-time monitoring of all instances with health checks and alerts.

### Step 5: Environment Variables

Create `.env.example`:
```bash
# Supabase (needs service key for admin access)
REACT_APP_SUPABASE_URL=https://xxx.supabase.co
REACT_APP_SUPABASE_SERVICE_KEY=eyJ...

# Dokku Provisioner
REACT_APP_PROVISIONER_URL=http://localhost:8002
REACT_APP_PROVISIONER_API_KEY=secret

# Stripe
REACT_APP_STRIPE_SECRET_KEY=sk_test_...
```

## Key Admin Features to Implement

1. **Customer Management**:
   - View all customers with search and filters
   - Edit customer details
   - Suspend/reactivate accounts
   - View customer history

2. **Subscription Management**:
   - Manual subscription changes
   - Apply credits/discounts
   - Handle failed payments
   - Upgrade/downgrade tiers

3. **Instance Operations**:
   - Start/stop/restart instances
   - View logs and metrics
   - Manual provisioning
   - Resource limit adjustments

4. **Platform Metrics**:
   - MRR and growth metrics
   - Usage trends
   - Instance health overview
   - Error rates and alerts

5. **Support Tools**:
   - View audit logs
   - Impersonate users
   - Generate reports
   - Bulk operations

## Security Considerations

1. **Authentication**: Use separate admin accounts with 2FA
2. **Authorization**: Role-based access (super admin, support, read-only)
3. **Audit Logging**: Log all admin actions
4. **IP Restrictions**: Limit admin access to specific IPs
5. **Service Key**: Keep Supabase service key secure

## Output Files Required

```
apps/admin-dashboard/
├── src/
│   ├── App.tsx
│   ├── Dashboard.tsx
│   ├── authProvider.ts
│   ├── dataProvider.ts
│   ├── resources/
│   ├── components/
│   └── services/
├── public/
├── .env.example
├── package.json
├── Dockerfile
└── README.md
```

## Important Notes

1. DO NOT modify any files outside `apps/admin-dashboard/`
2. Use Supabase service key for full database access
3. This is internal only - security is critical
4. Add proper error handling and logging
5. Use React Admin's built-in features where possible
6. Follow Material-UI design patterns
7. Make it efficient for support team workflows

Remember: This dashboard is for internal operations. Focus on functionality and efficiency over aesthetics.
