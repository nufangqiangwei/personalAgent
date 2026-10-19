import { describe, expect, it } from 'vitest'

import { routes } from '@/router'

describe('router shell', () => {
  it('exposes the phase-1 entry routes without phase-2 pages', () => {
    const names = routes.map((route) => route.name)
    expect(names).toContain('workflows')
    expect(names).toContain('workflow-versions')
    expect(names).toContain('requirements')
    expect(names).toContain('run-progress')
    expect(names).toContain('artifact')
    expect(names).toContain('runs')
    expect(names).toContain('setup')
    expect(names).toContain('data')
    expect(names).toContain('maintenance')
    expect(names).not.toContain('matching')
    expect(names).not.toContain('exploration')
    expect(names).not.toContain('preferences')
  })

  it('starts at the workflow list so the first action is manual selection', () => {
    expect(routes[0]?.path).toBe('/')
    expect(routes[0]?.name).toBe('workflows')
  })

  it('catches unknown paths so no blank screen is shown', () => {
    expect(routes.some((route) => route.name === 'not-found')).toBe(true)
  })
})
