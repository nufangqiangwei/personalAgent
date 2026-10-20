import { expect, test, type Page } from '@playwright/test'

/**
 * T099 progress/stop latency against the real local app.
 *
 * A research run in the mock mode finishes in a few hundred milliseconds, so a
 * run is first paused through the public API (with the same CSRF/idempotency
 * headers the UI uses), then the progress page is opened and the run is resumed
 * from the page. Every event committed after the resume is observed through the
 * committed event-history endpoint; the sample is the gap between the server's
 * ``occurred_at`` for that event and the first browser frame that renders its
 * sequence in the events list. Both clocks belong to the same machine.
 *
 * The stop test resumes a paused run and presses the pause control in the page.
 * The feedback sample is the browser-side gap between the click and the
 * server-derived ``draining`` / status message rendered by RunControls.
 */

const WORKFLOW_NAME = '公开主题资料研究'
const START_MESSAGE = '12 人团队想了解公共图书馆夜间开放'
const PROGRESS_BUDGET_MS = 1_000
const SAMPLE_TARGET = 20
const MAX_RUNS = 8

interface ProgressSample {
  sequence: number
  latencyMs: number
}

interface ProgressCollector {
  samples: ProgressSample[]
  stop: () => void
}

interface StopFeedback {
  clickedAt: number
  feedbackAt: number
  feedback: string
}

interface RunSummary {
  id: string
  revision: number
  status: string
  last_event_sequence: number
}

interface EventHistoryPage {
  items: { data: { sequence: number; occurred_at: string } }[]
}

function percentile(samples: number[], fraction: number): number {
  const ordered = [...samples].sort((left, right) => left - right)
  const index = Math.max(0, Math.ceil(fraction * ordered.length) - 1)
  return ordered[Math.min(index, ordered.length - 1)] ?? 0
}

function median(samples: number[]): number {
  const ordered = [...samples].sort((left, right) => left - right)
  const middle = Math.floor(ordered.length / 2)
  if (ordered.length % 2 === 1) return ordered[middle] ?? 0
  return ((ordered[middle - 1] ?? 0) + (ordered[middle] ?? 0)) / 2
}

async function startRun(page: Page): Promise<string> {
  return await page.evaluate(
    async ({ workflowName, message }) => {
      const cookie = (key: string): string =>
        document.cookie
          .split(';')
          .map((part) => part.trim())
          .find((part) => part.startsWith(`${key}=`))
          ?.split('=')[1] ?? ''

      const headers = (extra: Record<string, string> = {}): Record<string, string> => ({
        accept: 'application/json',
        'content-type': 'application/json',
        'x-csrf-token': decodeURIComponent(cookie('pa_csrf')),
        'idempotency-key': crypto.randomUUID(),
        ...extra,
      })

      async function call<T>(path: string, init?: RequestInit): Promise<T> {
        const response = await fetch(path, init)
        if (!response.ok) {
          throw new Error(`${path} -> ${response.status} ${await response.text()}`)
        }
        return (await response.json()) as T
      }

      const workflows = await call<{ items: { name: string; latest_version: { id: string } }[] }>(
        '/api/v1/workflows',
      )
      const version = workflows.items.find((item) => item.name === workflowName)?.latest_version
      if (!version) throw new Error(`workflow ${workflowName} is missing`)

      const draft = await call<{ id: string }>('/api/v1/requirements', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ workflow_version_id: version.id }),
      })
      await call(`/api/v1/requirements/${draft.id}/messages`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ text: message }),
      })
      const latest = await call<{
        revision: number
        summary_hash: string
        workflow_version_id: string
      }>(`/api/v1/requirements/${draft.id}`)
      const policies = await call<{ hash: string; version: string }>('/policies')
      const confirmation = await call<{ confirmation_id: string }>(
        `/api/v1/requirements/${draft.id}/confirm`,
        {
          method: 'POST',
          headers: headers({ 'if-match': `"${latest.revision}"` }),
          body: JSON.stringify({
            draft_revision: latest.revision,
            summary_hash: latest.summary_hash,
            workflow_version_id: latest.workflow_version_id,
            budget_policy_hash: policies.hash,
            data_policy_version: policies.version,
            action: 'START',
            explicit_start_confirmation: true,
          }),
        },
      )
      const started = await call<{ id: string }>('/api/v1/runs', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ confirmation_id: confirmation.confirmation_id }),
      })
      return started.id
    },
    { workflowName: WORKFLOW_NAME, message: START_MESSAGE },
  )
}

async function readStatus(page: Page, runId: string): Promise<string> {
  return await page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/runs/${id}`, { headers: { accept: 'application/json' } })
    if (!response.ok) return 'UNKNOWN'
    const snapshot = (await response.json()) as { status: string }
    return snapshot.status
  }, runId)
}

async function waitForStatus(
  page: Page,
  runId: string,
  accepted: string[],
  timeoutMs: number,
): Promise<string> {
  const deadline = Date.now() + timeoutMs
  let status = await readStatus(page, runId)
  while (!accepted.includes(status) && Date.now() < deadline) {
    await page.waitForTimeout(50)
    status = await readStatus(page, runId)
  }
  return status
}

/** Pause a fresh run before it terminates, so the page can resume it on demand. */
async function createPausedRun(page: Page): Promise<string> {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const runId = await startRun(page)
    const paused = await page.evaluate(async (id) => {
      const cookie = (key: string): string =>
        document.cookie
          .split(';')
          .map((part) => part.trim())
          .find((part) => part.startsWith(`${key}=`))
          ?.split('=')[1] ?? ''
      const snapshotResponse = await fetch(`/api/v1/runs/${id}`, {
        headers: { accept: 'application/json' },
      })
      if (!snapshotResponse.ok) return false
      const snapshot = (await snapshotResponse.json()) as { revision: number; status: string }
      if (snapshot.status !== 'RUNNING') return false
      const response = await fetch(`/api/v1/runs/${id}/pause`, {
        method: 'POST',
        headers: {
          accept: 'application/json',
          'content-type': 'application/json',
          'x-csrf-token': decodeURIComponent(cookie('pa_csrf')),
          'idempotency-key': crypto.randomUUID(),
          'if-match': `"${snapshot.revision}"`,
        },
        body: JSON.stringify({ revision: snapshot.revision }),
      })
      return response.ok
    }, runId)
    if (paused) {
      const status = await waitForStatus(page, runId, ['PAUSED'], 5_000)
      if (status === 'PAUSED') return runId
    }
    // The run won the race: let it terminate so the single execution slot is
    // free before the next attempt.
    await waitForStatus(page, runId, ['SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'], 5_000)
  }
  throw new Error('could not pause a fresh run before it terminated')
}

async function installProgressCollector(page: Page, runId: string): Promise<void> {
  await page.evaluate(async (id) => {
    const collector: ProgressCollector = { samples: [], stop: () => undefined }
    const committed = new Map<number, number>()
    const rendered = new Map<number, number>()
    let stopped = false
    let cursor = 0

    const snapshotResponse = await fetch(`/api/v1/runs/${id}`, {
      headers: { accept: 'application/json' },
    })
    if (snapshotResponse.ok) {
      const snapshot = (await snapshotResponse.json()) as RunSummary
      cursor = snapshot.last_event_sequence
    }

    const recordRendered = (): void => {
      if (stopped) return
      for (const element of Array.from(document.querySelectorAll('li'))) {
        const match = /^#(\d+)\s/.exec((element.textContent ?? '').trim())
        if (!match) continue
        const sequence = Number(match[1])
        if (!rendered.has(sequence)) {
          rendered.set(sequence, performance.timeOrigin + performance.now())
        }
      }
    }

    const observer = new MutationObserver(recordRendered)
    observer.observe(document.body, { subtree: true, childList: true, characterData: true })

    let busy = false
    const timer = window.setInterval(() => {
      if (stopped || busy) return
      busy = true
      void (async () => {
        try {
          const response = await fetch(
            `/api/v1/runs/${id}/event-history?after=${cursor}&limit=100`,
            { headers: { accept: 'application/json' } },
          )
          if (response.ok) {
            const page = (await response.json()) as EventHistoryPage
            for (const item of page.items) {
              const sequence = item.data.sequence
              cursor = Math.max(cursor, sequence)
              if (!committed.has(sequence)) {
                committed.set(sequence, Date.parse(item.data.occurred_at))
              }
            }
          }
        } catch {
          // The run is still being written; the next poll retries.
        } finally {
          busy = false
        }
        recordRendered()
      })()
    }, 25)

    collector.stop = (): void => {
      recordRendered()
      stopped = true
      window.clearInterval(timer)
      observer.disconnect()
      collector.samples = [...committed.entries()]
        .filter(([sequence]) => rendered.has(sequence))
        .map(([sequence]) => ({
          sequence,
          latencyMs: (rendered.get(sequence) ?? 0) - (committed.get(sequence) ?? 0),
        }))
        .sort((left, right) => left.sequence - right.sequence)
    }
    ;(window as unknown as { __paProgress?: ProgressCollector }).__paProgress = collector
  }, runId)
}

async function stopProgressCollector(page: Page): Promise<ProgressSample[]> {
  return await page.evaluate(() => {
    const holder = window as unknown as { __paProgress?: ProgressCollector }
    holder.__paProgress?.stop()
    return holder.__paProgress?.samples ?? []
  })
}

test('committed progress events render within one second p95', async ({ page }) => {
  await page.goto('/')
  const samples: ProgressSample[] = []

  for (let run = 0; run < MAX_RUNS && samples.length < SAMPLE_TARGET; run += 1) {
    const runId = await createPausedRun(page)
    await page.goto(`/runs/${runId}`)
    await expect(page.locator('.summary dd').first()).toContainText('PAUSED')

    await installProgressCollector(page, runId)
    await page.getByRole('button', { name: '继续（同 ID）' }).click()
    const status = await waitForStatus(
      page,
      runId,
      ['SUCCEEDED', 'FAILED', 'CANCELLED'],
      30_000,
    )
    expect(['SUCCEEDED', 'FAILED', 'CANCELLED']).toContain(status)
    // Let the last server-sent frames reach the events list before freezing.
    await page.waitForTimeout(300)
    samples.push(...(await stopProgressCollector(page)))
  }

  const latencies = samples.map((sample) => sample.latencyMs)
  expect(
    latencies.length,
    `only ${latencies.length} committed events were observed in ${MAX_RUNS} runs`,
  ).toBeGreaterThanOrEqual(SAMPLE_TARGET)

  const measuredP95 = percentile(latencies, 0.95)
  console.log(
    `T099 progress: n=${latencies.length} p50=${median(latencies).toFixed(0)}ms ` +
      `p95=${measuredP95.toFixed(0)}ms`,
  )
  expect(measuredP95).toBeLessThan(PROGRESS_BUDGET_MS)
})

test('pressing stop shows server feedback within one second', async ({ page }) => {
  await page.goto('/')

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const runId = await createPausedRun(page)
    await page.goto(`/runs/${runId}`)
    await expect(page.locator('.summary dd').first()).toContainText('PAUSED')

    await page.evaluate(() => {
      const state: StopFeedback = { clickedAt: 0, feedbackAt: 0, feedback: '' }
      ;(window as unknown as { __paStop?: StopFeedback }).__paStop = state
      document.addEventListener(
        'click',
        (event) => {
          const button = (event.target as HTMLElement).closest('button')
          if (button?.textContent?.trim() === '暂停') {
            state.clickedAt = performance.now()
          }
        },
        true,
      )
      const readFeedback = (): void => {
        if (state.clickedAt === 0 || state.feedbackAt !== 0) return
        const controls = document.querySelector('.controls')
        if (!controls) return
        const feedback = Array.from(controls.querySelectorAll('[role="status"]'))
          .map((element) => (element as HTMLElement).innerText.trim())
          .filter(Boolean)
          .join(' | ')
        if (feedback) {
          state.feedbackAt = performance.now()
          state.feedback = feedback
        }
      }
      new MutationObserver(readFeedback).observe(document.body, {
        subtree: true,
        childList: true,
        characterData: true,
      })
      window.setInterval(readFeedback, 10)
    })

    await page.getByRole('button', { name: '继续（同 ID）' }).click()
    const pauseButton = page.getByRole('button', { name: '暂停' })
    try {
      await expect(pauseButton).toBeEnabled({ timeout: 2_000 })
    } catch {
      // The resumed run finished before the pause could be pressed; let it
      // settle so the single execution slot is free, then start over.
      await waitForStatus(page, runId, ['SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'], 5_000)
      continue
    }
    await pauseButton.click()

    await expect
      .poll(
        async () =>
          await page.evaluate(() => {
            const holder = window as unknown as { __paStop?: StopFeedback }
            return holder.__paStop?.feedbackAt ?? 0
          }),
        { timeout: 3_000 },
      )
      .toBeGreaterThan(0)

    const feedback = await page.evaluate(() => {
      const holder = window as unknown as { __paStop?: StopFeedback }
      return holder.__paStop ?? { clickedAt: 0, feedbackAt: 0, feedback: '' }
    })
    const latency = feedback.feedbackAt - feedback.clickedAt
    console.log(`T099 stop: feedback=${latency.toFixed(0)}ms text=${feedback.feedback}`)
    expect(latency).toBeGreaterThanOrEqual(0)
    expect(latency).toBeLessThan(PROGRESS_BUDGET_MS)
    // The text comes from the server snapshot: the draining hint for an
    // in-flight action, or the converged status after the pause took effect.
    expect(feedback.feedback).toMatch(/正在收敛|状态已更新为 PAUSED/)
    await waitForStatus(page, runId, ['PAUSED', 'SUCCEEDED', 'FAILED', 'CANCELLED'], 5_000)
    return
  }

  throw new Error('could not press pause while a resumed run was still running')
})
