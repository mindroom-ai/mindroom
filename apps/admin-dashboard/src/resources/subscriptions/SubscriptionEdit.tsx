import {
  Edit,
  SimpleForm,
  TextInput,
  SelectInput,
  NumberInput,
  DateTimeInput,
  ReferenceInput,
  required,
} from 'react-admin'

export const SubscriptionEdit = () => (
  <Edit>
    <SimpleForm>
      <TextInput source="id" disabled />
      <ReferenceInput source="account_id" reference="accounts">
        <SelectInput optionText="email" disabled />
      </ReferenceInput>
      <SelectInput
        source="tier"
        choices={[
          { id: 'free', name: 'Free' },
          { id: 'starter', name: 'Starter' },
          { id: 'professional', name: 'Professional' },
          { id: 'enterprise', name: 'Enterprise' },
        ]}
        validate={required()}
      />
      <SelectInput
        source="status"
        choices={[
          { id: 'active', name: 'Active' },
          { id: 'cancelled', name: 'Cancelled' },
          { id: 'expired', name: 'Expired' },
          { id: 'trial', name: 'Trial' },
          { id: 'pending', name: 'Pending' },
        ]}
        validate={required()}
      />
      <TextInput source="stripe_subscription_id" />
      <TextInput source="stripe_customer_id" />

      <NumberInput source="max_agents" min={1} />
      <NumberInput source="max_messages_per_day" min={1} />
      <NumberInput source="max_storage_gb" min={1} />
      <NumberInput source="max_api_calls_per_month" min={1} />

      <NumberInput source="current_messages_today" min={0} />
      <NumberInput source="current_storage_bytes" min={0} />
      <NumberInput source="current_api_calls_this_month" min={0} />

      <DateTimeInput source="trial_ends_at" />
      <DateTimeInput source="current_period_start" />
      <DateTimeInput source="current_period_end" />
      <DateTimeInput source="cancelled_at" />
      <DateTimeInput source="usage_reset_at" />

      <DateTimeInput source="created_at" disabled />
      <DateTimeInput source="updated_at" disabled />
    </SimpleForm>
  </Edit>
)
