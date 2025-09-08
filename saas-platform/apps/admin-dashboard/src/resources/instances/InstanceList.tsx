import {
  List,
  Datagrid,
  TextField,
  DateField,
  ShowButton,
  SearchInput,
  SelectInput,
  TopToolbar,
  CreateButton,
  ExportButton,
  FilterButton,
  useNotify,
  useRefresh,
  ReferenceField,
  NumberField,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'
import { Button, ButtonGroup } from '@mui/material'
import { Play, Pause, RotateCw } from 'lucide-react'
import { useCallback } from 'react'

const InstanceActions = ({ record }: any) => {
  const notify = useNotify()
  const refresh = useRefresh()

  const handleAction = useCallback(async (action: string) => {
    try {
      const response = await fetch(`/api/instances/${record.id}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
  }, [record, notify, refresh])

  return (
    <ButtonGroup size="small" variant="outlined">
      {record.status === 'running' ? (
        <Button
          onClick={() => handleAction('stop')}
          startIcon={<Pause className="w-4 h-4" />}
        >
          Stop
        </Button>
      ) : (
        <Button
          onClick={() => handleAction('start')}
          startIcon={<Play className="w-4 h-4" />}
        >
          Start
        </Button>
      )}
      <Button
        onClick={() => handleAction('restart')}
        startIcon={<RotateCw className="w-4 h-4" />}
      >
        Restart
      </Button>
      <ShowButton />
    </ButtonGroup>
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
      { id: 'deprovisioning', name: 'Deprovisioning' },
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
    <Datagrid>
      <TextField source="name" />
      <ReferenceField source="account_id" reference="accounts" link="show">
        <TextField source="email" />
      </ReferenceField>
      <TextField source="subdomain" />
      <StatusChip source="status" />
      <NumberField source="memory_limit_mb" label="Memory (MB)" />
      <TextField source="cpu_limit" label="CPU" />
      <DateField source="provisioned_at" showTime />
      <InstanceActions />
    </Datagrid>
  </List>
)
