import {
  Edit,
  SimpleForm,
  TextInput,
  SelectInput,
  BooleanInput,
  DateTimeInput,
  required,
} from 'react-admin'

export const AccountEdit = () => (
  <Edit>
    <SimpleForm>
      <TextInput source="id" disabled />
      <TextInput source="email" validate={required()} />
      <TextInput source="full_name" />
      <TextInput source="company_name" />
      <TextInput source="phone" />
      <SelectInput
        source="status"
        choices={[
          { id: 'active', name: 'Active' },
          { id: 'suspended', name: 'Suspended' },
          { id: 'deleted', name: 'Deleted' },
          { id: 'pending_verification', name: 'Pending Verification' },
        ]}
        validate={required()}
      />
      <BooleanInput source="email_verified" />
      <BooleanInput source="two_factor_enabled" label="2FA Enabled" />
      <DateTimeInput source="last_login" disabled />
      <DateTimeInput source="created_at" disabled />
      <DateTimeInput source="updated_at" disabled />
    </SimpleForm>
  </Edit>
)
