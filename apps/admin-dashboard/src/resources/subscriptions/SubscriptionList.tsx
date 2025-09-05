import {
  List,
  Datagrid,
  TextField,
  ReferenceField,
  DateField,
  NumberField,
  EditButton,
  ShowButton,
  SearchInput,
  SelectInput,
  TopToolbar,
  ExportButton,
  FilterButton,
  BulkDeleteButton,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'

const subscriptionFilters = [
  <SearchInput source="q" alwaysOn />,
  <SelectInput
    source="tier"
    choices={[
      { id: 'free', name: 'Free' },
      { id: 'starter', name: 'Starter' },
      { id: 'professional', name: 'Professional' },
      { id: 'enterprise', name: 'Enterprise' },
    ]}
  />,
  <SelectInput
    source="status"
    choices={[
      { id: 'active', name: 'Active' },
      { id: 'cancelled', name: 'Cancelled' },
      { id: 'expired', name: 'Expired' },
      { id: 'trial', name: 'Trial' },
      { id: 'pending', name: 'Pending' },
    ]}
  />,
]

const ListActions = () => (
  <TopToolbar>
    <FilterButton />
    <ExportButton />
  </TopToolbar>
)

export const SubscriptionList = () => (
  <List
    filters={subscriptionFilters}
    actions={<ListActions />}
    sort={{ field: 'created_at', order: 'DESC' }}
  >
    <Datagrid bulkActionButtons={<BulkDeleteButton />}>
      <ReferenceField source="account_id" reference="accounts">
        <TextField source="email" />
      </ReferenceField>
      <TextField source="tier" />
      <StatusChip source="status" />
      <NumberField source="max_agents" />
      <NumberField source="max_messages_per_day" />
      <NumberField source="max_storage_gb" label="Storage (GB)" />
      <DateField source="current_period_end" />
      <DateField source="created_at" showTime />
      <ShowButton />
      <EditButton />
    </Datagrid>
  </List>
)
