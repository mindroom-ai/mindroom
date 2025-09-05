import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from 'recharts'
import { Card, CardContent, CardHeader, Typography } from '@mui/material'

interface UsageChartProps {
  title: string
  data: any[]
  dataKeys: string[]
  colors?: string[]
}

export const UsageChart = ({ title, data, dataKeys, colors = ['#f97316', '#10b981', '#3b82f6'] }: UsageChartProps) => {
  return (
    <Card>
      <CardHeader
        title={<Typography variant="h6">{title}</Typography>}
      />
      <CardContent>
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="date" />
            <YAxis />
            <Tooltip />
            <Legend />
            {dataKeys.map((key, index) => (
              <Line
                key={key}
                type="monotone"
                dataKey={key}
                stroke={colors[index % colors.length]}
                strokeWidth={2}
                dot={{ r: 3 }}
                activeDot={{ r: 5 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}
