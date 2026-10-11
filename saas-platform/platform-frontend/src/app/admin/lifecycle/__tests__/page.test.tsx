import { render, screen, waitFor } from '@testing-library/react'
import LifecyclePage from '../page'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({
  apiCall: jest.fn(),
}))

const pendingInstance = {
  instance_id: 7,
  account_id: 'acc-1',
  account_email: 'customer@example.com',
  subscription_status: 'cancelled',
  status: 'stopped',
  lifecycle_stopped_at: '2026-09-20T03:00:00+00:00',
  teardown_after: '2026-10-20T03:00:00+00:00',
  lifecycle_error: null,
  lifecycle_error_at: null,
  problem: null,
}

function mockLifecycle(body: Record<string, unknown>) {
  ;(apiCall as jest.Mock).mockResolvedValue({ ok: true, json: async () => body })
}

describe('LifecyclePage', () => {
  beforeEach(() => {
    jest.clearAllMocks()
  })

  it('shows the last run, stuck instances, and pending teardowns', async () => {
    mockLifecycle({
      cleanup_scheduler_enabled: true,
      teardown_grace_days: 30,
      last_run: {
        started_at: '2026-09-27T03:00:00+00:00',
        finished_at: '2026-09-27T03:01:00+00:00',
        ok: false,
        summary: {
          accounts: { error: 'rpc denied' },
          instance_lifecycle: { subscriptions_checked: 4, instances_stopped: 1, errors: ['instance 8: kubectl failed'] },
        },
      },
      pending_teardown: [pendingInstance],
      stuck: [{ ...pendingInstance, instance_id: 8, status: 'running', problem: 'Teardown is overdue' }],
    })

    render(<LifecyclePage />)

    await waitFor(() => expect(screen.getByText('Failed')).toBeInTheDocument())
    expect(apiCall).toHaveBeenCalledWith('/admin/instance-lifecycle')
    expect(screen.getByText('accounts: rpc denied')).toBeInTheDocument()
    expect(screen.getByText('instance 8: kubectl failed')).toBeInTheDocument()
    expect(screen.getByText('Needs attention (1)')).toBeInTheDocument()
    expect(screen.getByText('Teardown is overdue')).toBeInTheDocument()
    expect(screen.getByText('Pending teardown (1)')).toBeInTheDocument()
    expect(screen.getAllByText('customer@example.com')).toHaveLength(2)
    expect(screen.queryByText(/cleanup scheduler is disabled/)).not.toBeInTheDocument()
  })

  it('warns when the scheduler is disabled and no run exists', async () => {
    mockLifecycle({
      cleanup_scheduler_enabled: false,
      teardown_grace_days: 30,
      last_run: null,
      pending_teardown: [],
      stuck: [],
    })

    render(<LifecyclePage />)

    await waitFor(() => expect(screen.getByText(/cleanup scheduler is disabled/)).toBeInTheDocument())
    expect(screen.getByText('The nightly cleanup has not run yet.')).toBeInTheDocument()
  })
})
