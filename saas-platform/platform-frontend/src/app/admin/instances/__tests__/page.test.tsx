import { render, screen } from '@testing-library/react'
import InstancesPage from '../page'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({ apiCall: jest.fn() }))

describe('InstancesPage', () => {
  it('offers only the instance refresh beside the instance list', async () => {
    ;(apiCall as jest.Mock).mockResolvedValue({ ok: true, json: async () => ({ data: [], total: 0 }) })

    render(<InstancesPage />)

    expect(await screen.findByText('All Instances')).toBeInTheDocument()
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Refresh All'])
  })
})
