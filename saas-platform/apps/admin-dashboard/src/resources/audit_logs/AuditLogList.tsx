import {
  List,
  Datagrid,
  TextField,
  DateField,
  ReferenceField,
  SearchInput,
  SelectInput,
  TopToolbar,
  ExportButton,
  FilterButton,
  DateInput,
} from 'react-admin'

const auditLogFilters = [
  <SearchInput source="q" alwaysOn />,
  <SelectInput
    source="action"
    choices={[
      { id: 'login', name: 'Login' },
      { id: 'logout', name: 'Logout' },
      { id: 'create', name: 'Create' },
      { id: 'update', name: 'Update' },
      { id: 'delete', name: 'Delete' },
      { id: 'provision', name: 'Provision' },
      { id: 'deprovision', name: 'Deprovision' },
    ]}
  />,
  <SelectInput
    source="resource_type"
    choices={[
      { id: 'account', name: 'Account' },
      { id: 'subscription', name: 'Subscription' },
      { id: 'instance', name: 'Instance' },
      { id: 'api_key', name: 'API Key' },
    ]}
  />,
  <DateInput source="created_at_gte" label="From Date" />,
  <DateInput source="created_at_lte" label="To Date" />,
]

const ListActions = () => (
  <TopToolbar>
    <FilterButton />
    <ExportButton />
  </TopToolbar>
)

export const AuditLogList = () => (
  <List
    filters={auditLogFilters}
    actions={<ListActions />}
    sort={{ field: 'created_at', order: 'DESC' }}
    perPage={50}
  >
    <Datagrid>
      <DateField source="created_at" showTime />
      <ReferenceField source="account_id" reference="accounts" link="show">
        <TextField source="email" />
      </ReferenceField>
      <TextField source="action" />
      <TextField source="resource_type" />
      <TextField source="resource_id" />
      <TextField source="ip_address" />
      <TextField source="request_id" />
      <TextField source="changes" />
    </Datagrid>
  </List>
)
