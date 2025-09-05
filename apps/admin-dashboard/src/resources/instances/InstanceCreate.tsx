import {
  Create,
  SimpleForm,
  TextInput,
  SelectInput,
  NumberInput,
  ReferenceInput,
  BooleanInput,
  required,
  useNotify,
  useRedirect,
} from 'react-admin'
import { Card, CardContent, Typography } from '@mui/material'

export const InstanceCreate = () => {
  const notify = useNotify()
  const redirect = useRedirect()

  const handleSave = async (data: any) => {
    try {
      // Call the provisioner API
      const response = await fetch('/api/instances/provision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })

      if (response.ok) {
        notify('Instance provisioning started', { type: 'success' })
        redirect('list', 'instances')
      } else {
        throw new Error('Provisioning failed')
      }
    } catch (error) {
      notify('Failed to provision instance', { type: 'error' })
    }
  }

  return (
    <Create>
      <SimpleForm onSubmit={handleSave}>
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>Account Information</Typography>
            <ReferenceInput source="account_id" reference="accounts" validate={required()}>
              <SelectInput optionText="email" />
            </ReferenceInput>
          </CardContent>
        </Card>

        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>Instance Configuration</Typography>
            <TextInput source="name" validate={required()} fullWidth />
            <TextInput source="subdomain" validate={required()} fullWidth />
            <SelectInput
              source="status"
              choices={[
                { id: 'provisioning', name: 'Provisioning' },
                { id: 'running', name: 'Running' },
                { id: 'stopped', name: 'Stopped' },
              ]}
              defaultValue="provisioning"
              validate={required()}
            />
          </CardContent>
        </Card>

        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>Resource Limits</Typography>
            <TextInput
              source="cpu_limit"
              defaultValue="0.5"
              helperText="e.g., '0.5' for half a CPU core"
            />
            <NumberInput
              source="memory_limit_mb"
              defaultValue={512}
              min={256}
              step={256}
              helperText="Memory limit in MB"
            />
            <NumberInput
              source="storage_limit_gb"
              defaultValue={5}
              min={1}
              helperText="Storage limit in GB"
            />
          </CardContent>
        </Card>

        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>Matrix Configuration</Typography>
            <BooleanInput source="matrix_enabled" defaultValue={false} />
            <SelectInput
              source="matrix_type"
              choices={[
                { id: 'synapse', name: 'Synapse' },
                { id: 'conduit', name: 'Conduit' },
              ]}
              defaultValue="conduit"
            />
            <TextInput source="matrix_server_name" fullWidth />
          </CardContent>
        </Card>

        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>Network Configuration (Optional)</Typography>
            <NumberInput source="backend_port" />
            <NumberInput source="frontend_port" />
            <NumberInput source="matrix_port" />
            <TextInput source="data_dir" fullWidth />
          </CardContent>
        </Card>
      </SimpleForm>
    </Create>
  )
}
