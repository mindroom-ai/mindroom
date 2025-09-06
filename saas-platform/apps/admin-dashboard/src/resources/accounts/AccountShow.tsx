import {
  Show,
  TextField,
  EmailField,
  DateField,
  BooleanField,
  ReferenceManyField,
  Datagrid,
  EditButton,
  TabbedShowLayout,
  Tab,
  NumberField,
  ShowButton,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'
import { Card, CardContent, Typography, Box } from '@mui/material'

export const AccountShow = () => (
  <Show>
    <TabbedShowLayout>
      <Tab label="Overview">
        <TextField source="id" />
        <EmailField source="email" />
        <TextField source="full_name" />
        <TextField source="company_name" />
        <TextField source="phone" />
        <StatusChip source="status" />
        <BooleanField source="email_verified" />
        <BooleanField source="two_factor_enabled" label="2FA Enabled" />
        <DateField source="created_at" showTime />
        <DateField source="updated_at" showTime />
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
            <NumberField source="max_agents" />
            <NumberField source="max_messages_per_day" />
            <NumberField source="max_storage_gb" label="Storage (GB)" />
            <DateField source="current_period_end" />
            <EditButton />
          </Datagrid>
        </ReferenceManyField>
      </Tab>

      <Tab label="Instances">
        <ReferenceManyField
          label="Instances"
          reference="instances"
          target="account_id"
        >
          <Datagrid>
            <TextField source="name" />
            <TextField source="subdomain" />
            <StatusChip source="status" />
            <TextField source="memory_limit" label="Memory" />
            <TextField source="cpu_limit" label="CPU" />
            <DateField source="provisioned_at" showTime />
            <ShowButton />
          </Datagrid>
        </ReferenceManyField>
      </Tab>

      <Tab label="Usage">
        <ReferenceManyField
          label="Recent Usage"
          reference="usage_metrics"
          target="account_id"
          sort={{ field: 'metric_date', order: 'DESC' }}
        >
          <Datagrid>
            <DateField source="metric_date" />
            <NumberField source="messages_sent" />
            <NumberField source="api_calls" />
            <NumberField source="active_agents" />
            <NumberField source="compute_seconds" />
          </Datagrid>
        </ReferenceManyField>
      </Tab>

      <Tab label="API Keys">
        <ReferenceManyField
          label="API Keys"
          reference="api_keys"
          target="account_id"
        >
          <Datagrid>
            <TextField source="name" />
            <TextField source="key_prefix" />
            <BooleanField source="is_active" />
            <DateField source="last_used_at" showTime />
            <DateField source="expires_at" showTime />
            <DateField source="created_at" showTime />
            <EditButton />
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
            <TextField source="resource_type" />
            <TextField source="ip_address" />
          </Datagrid>
        </ReferenceManyField>
      </Tab>
    </TabbedShowLayout>
  </Show>
)
