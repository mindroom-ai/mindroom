'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { apiCall, type InstanceLifecycle } from '@/lib/api'
import { logger } from '@/lib/logger'

type LifecycleInstance = InstanceLifecycle['pending_teardown'][number]

const LIFECYCLE_COUNTS = [
  ['subscriptions_checked', 'Subscriptions checked'],
  ['instances_stopped', 'Instances stopped'],
  ['instances_resumed', 'Instances resumed'],
  ['instances_torn_down', 'Instances torn down'],
  ['subscriptions_paused', 'Trials paused'],
] as const

function formatDateTime(value?: string | null) {
  return value ? new Date(value).toLocaleString() : '-'
}

function lifecycleSummary(summary: Record<string, unknown>) {
  const lifecycle = summary.instance_lifecycle
  return lifecycle && typeof lifecycle === 'object' ? (lifecycle as Record<string, unknown>) : {}
}

function taskErrors(summary: Record<string, unknown>) {
  const errors: string[] = []
  for (const [task, result] of Object.entries(summary)) {
    if (!result || typeof result !== 'object') continue
    const { error, errors: itemErrors } = result as { error?: unknown; errors?: unknown }
    if (typeof error === 'string') errors.push(`${task}: ${error}`)
    if (Array.isArray(itemErrors)) errors.push(...itemErrors.map(String))
  }
  return errors
}

function InstanceTable({ rows, showProblem }: { rows: LifecycleInstance[]; showProblem: boolean }) {
  if (rows.length === 0) {
    return <div className="text-center py-6 text-gray-500 dark:text-gray-400">None</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-gray-700 dark:text-gray-300">
            <th className="py-2 px-3">Instance</th>
            <th className="py-2 px-3">Customer</th>
            <th className="py-2 px-3">Subscription</th>
            <th className="py-2 px-3">Status</th>
            <th className="py-2 px-3">Stopped</th>
            <th className="py-2 px-3">Teardown after</th>
            {showProblem && <th className="py-2 px-3">Problem</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.instance_id)} className="border-b border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100">
              <td className="py-2 px-3 font-mono">{row.instance_id}</td>
              <td className="py-2 px-3">{row.account_email || '-'}</td>
              <td className="py-2 px-3">{row.subscription_status || '-'}</td>
              <td className="py-2 px-3">{row.status}</td>
              <td className="py-2 px-3">{formatDateTime(row.lifecycle_stopped_at)}</td>
              <td className="py-2 px-3">{formatDateTime(row.teardown_after)}</td>
              {showProblem && <td className="py-2 px-3 text-red-700 dark:text-red-400">{row.problem}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function LifecyclePage() {
  const [data, setData] = useState<InstanceLifecycle | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    const load = async () => {
      try {
        const response = await apiCall('/admin/instance-lifecycle')
        if (!response.ok) {
          setLoadError(`Failed to load lifecycle status (${response.status})`)
          return
        }
        setData(await response.json())
      } catch (error) {
        logger.error('Error fetching instance lifecycle:', error)
        setLoadError('Failed to load lifecycle status')
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [])

  if (loading) {
    return <div className="text-lg">Loading...</div>
  }
  if (!data) {
    return <div className="text-red-700 dark:text-red-400">{loadError}</div>
  }

  const lastRun = data.last_run
  const lifecycle = lastRun ? lifecycleSummary(lastRun.summary) : {}
  const errors = lastRun ? taskErrors(lastRun.summary) : []

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Instance Lifecycle</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-2">
          Instances of inactive subscriptions are stopped and torn down {data.teardown_grace_days} days later.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Last nightly cleanup</CardTitle>
        </CardHeader>
        <CardContent>
          {!data.cleanup_scheduler_enabled && (
            <p className="mb-3 font-semibold text-red-700 dark:text-red-400">
              The cleanup scheduler is disabled (ENABLE_CLEANUP_SCHEDULER), so the nightly job does not run.
            </p>
          )}
          {lastRun ? (
            <div className="space-y-3 text-sm text-gray-900 dark:text-gray-100">
              <p>
                {lastRun.ok ? (
                  <span className="font-semibold text-green-700 dark:text-green-400">Succeeded</span>
                ) : (
                  <span className="font-semibold text-red-700 dark:text-red-400">Failed</span>
                )}{' '}
                at {formatDateTime(lastRun.finished_at)}
              </p>
              <ul className="grid grid-cols-2 gap-2 md:grid-cols-5">
                {LIFECYCLE_COUNTS.map(([key, label]) => (
                  <li key={key}>
                    <div className="text-gray-500 dark:text-gray-400">{label}</div>
                    <div className="text-lg font-semibold">{String(lifecycle[key] ?? '-')}</div>
                  </li>
                ))}
              </ul>
              {errors.length > 0 && (
                <ul className="list-disc pl-5 text-red-700 dark:text-red-400">
                  {errors.map((error) => (
                    <li key={error}>{error}</li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <p className="text-sm text-gray-500 dark:text-gray-400">The nightly cleanup has not run yet.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Needs attention ({data.stuck.length})</CardTitle>
        </CardHeader>
        <CardContent>
          <InstanceTable rows={data.stuck} showProblem />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Pending teardown ({data.pending_teardown.length})</CardTitle>
        </CardHeader>
        <CardContent>
          <InstanceTable rows={data.pending_teardown} showProblem={false} />
        </CardContent>
      </Card>
    </div>
  )
}
