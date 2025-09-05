import {
  Show,
  SimpleShowLayout,
  TextField,
  ReferenceField,
  DateField,
  NumberField,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'
import { Typography, Box, Card, CardContent } from '@mui/material'

export const SubscriptionShow = () => (
  <Show>
    <SimpleShowLayout>
      <TextField source="id" />
      <ReferenceField source="account_id" reference="accounts" link="show">
        <TextField source="email" />
      </ReferenceField>
      <TextField source="tier" />
      <StatusChip source="status" />
      <TextField source="stripe_subscription_id" />
      <TextField source="stripe_customer_id" />

      <Box sx={{ mt: 2, mb: 2 }}>
        <Typography variant="h6" gutterBottom>Limits</Typography>
        <Card>
          <CardContent>
            <NumberField source="max_agents" label="Max Agents" />
            <NumberField source="max_messages_per_day" label="Max Messages/Day" />
            <NumberField source="max_storage_gb" label="Max Storage (GB)" />
            <NumberField source="max_api_calls_per_month" label="Max API Calls/Month" />
          </CardContent>
        </Card>
      </Box>

      <Box sx={{ mt: 2, mb: 2 }}>
        <Typography variant="h6" gutterBottom>Current Usage</Typography>
        <Card>
          <CardContent>
            <NumberField source="current_messages_today" label="Messages Today" />
            <NumberField source="current_storage_bytes" label="Storage Used (bytes)" />
            <NumberField source="current_api_calls_this_month" label="API Calls This Month" />
            <DateField source="usage_reset_at" showTime label="Usage Resets At" />
          </CardContent>
        </Card>
      </Box>

      <DateField source="trial_ends_at" showTime />
      <DateField source="current_period_start" showTime />
      <DateField source="current_period_end" showTime />
      <DateField source="cancelled_at" showTime />
      <DateField source="created_at" showTime />
      <DateField source="updated_at" showTime />
    </SimpleShowLayout>
  </Show>
)
