import {
  Admin,
  Resource,
  ListGuesser,
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
    mode: 'light' as const,
    primary: {
      main: '#f97316', // Orange to match MindRoom brand
    },
    secondary: {
      main: '#ea580c',
    },
    background: {
      default: '#fafafa',
    }
  },
  typography: {
    fontFamily: '"Inter", "Roboto", "Helvetica", "Arial", sans-serif',
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
        create={instances.InstanceCreate}
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
