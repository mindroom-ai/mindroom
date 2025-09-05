import {
  Show,
  SimpleShowLayout,
  TextField,
  DateField,
  NumberField,
  ReferenceField,
  BooleanField,
} from 'react-admin'
import { StatusChip } from '../../components/StatusChip'
import { Typography, Box, Card, CardContent, Grid } from '@mui/material'

export const InstanceShow = () => (
  <Show>
    <SimpleShowLayout>
      <Grid container spacing={2}>
        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Instance Details</Typography>
              <TextField source="id" />
              <ReferenceField source="account_id" reference="accounts" link="show">
                <TextField source="email" />
              </ReferenceField>
              <TextField source="name" />
              <TextField source="subdomain" />
              <StatusChip source="status" />
              <TextField source="dokku_app_name" label="Dokku App Name" />
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Network Configuration</Typography>
              <NumberField source="backend_port" />
              <NumberField source="frontend_port" />
              <NumberField source="matrix_port" />
              <TextField source="data_dir" />
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Resource Limits</Typography>
              <TextField source="cpu_limit" label="CPU Limit" />
              <NumberField source="memory_limit_mb" label="Memory Limit (MB)" />
              <NumberField source="storage_limit_gb" label="Storage Limit (GB)" />
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Matrix Configuration</Typography>
              <BooleanField source="matrix_enabled" />
              <TextField source="matrix_type" />
              <TextField source="matrix_server_name" />
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Health Status</Typography>
              <TextField source="health_status" />
              <DateField source="last_health_check" showTime />
              <NumberField source="uptime_seconds" label="Uptime (seconds)" />
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Timestamps</Typography>
              <DateField source="provisioned_at" showTime />
              <DateField source="deprovisioned_at" showTime />
              <DateField source="created_at" showTime />
              <DateField source="updated_at" showTime />
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Configuration</Typography>
              <Box sx={{ mt: 2 }}>
                <Typography variant="subtitle2" gutterBottom>Config JSON</Typography>
                <TextField source="config" />
              </Box>
              <Box sx={{ mt: 2 }}>
                <Typography variant="subtitle2" gutterBottom>Environment Variables</Typography>
                <TextField source="environment_vars" />
              </Box>
              <Box sx={{ mt: 2 }}>
                <Typography variant="subtitle2" gutterBottom>Metadata</Typography>
                <TextField source="metadata" />
              </Box>
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </SimpleShowLayout>
  </Show>
)
