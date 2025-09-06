import {
  List,
  Datagrid,
  TextField,
  EmailField,
  DateField,
  BooleanField,
  EditButton,
  ShowButton,
  SearchInput,
  SelectInput,
  TopToolbar,
  ExportButton,
  FilterButton,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'

const accountFilters = [
  <SearchInput source="q" alwaysOn />,
  <SelectInput
    source="status"
    choices={[
      { id: 'active', name: 'Active' },
      { id: 'suspended', name: 'Suspended' },
      { id: 'deleted', name: 'Deleted' },
      { id: 'pending_verification', name: 'Pending Verification' },
    ]}
  />,
  <SelectInput
    source="email_verified"
    label="Email Verified"
    choices={[
      { id: true, name: 'Yes' },
      { id: false, name: 'No' },
    ]}
  />,
]

const ListActions = () => (
  <TopToolbar>
    <FilterButton />
    <ExportButton />
  </TopToolbar>
)

export const AccountList = () => (
  <List
    filters={accountFilters}
    actions={<ListActions />}
    sort={{ field: 'created_at', order: 'DESC' }}
  >
    <Datagrid>
      <EmailField source="email" />
      <TextField source="full_name" />
      <TextField source="company_name" />
      <StatusChip source="status" />
      <BooleanField source="email_verified" />
      <DateField source="created_at" showTime />
      <DateField source="last_login" showTime />
      <ShowButton />
      <EditButton />
    </Datagrid>
  </List>
)
