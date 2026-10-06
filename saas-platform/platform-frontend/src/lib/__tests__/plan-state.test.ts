import { planState } from '../plan-state'

describe('planState', () => {
  it('treats a missing subscription and the free tier as no plan', () => {
    expect(planState(null)).toBe('none')
    expect(planState(undefined)).toBe('none')
    expect(planState({ tier: 'free', can_run_instances: false })).toBe('none')
  })

  it('separates a plan that runs from one that cannot', () => {
    expect(planState({ tier: 'hobby', can_run_instances: true })).toBe('active')
    expect(planState({ tier: 'hobby', can_run_instances: false })).toBe('lapsed')
  })
})
