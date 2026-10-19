import { expect, test, type Page } from '@playwright/test'

interface StartedRun {
  runId: string
  artifactId: string | null
  status: string
  completeness: string | null
}

/**
 * Start one research run through the public API from the browser context.
 *
 * The confirmation and start endpoints are used exactly as the UI does (CSRF
 * header, idempotency key, If-Match); the run is polled to a terminal state so the
 * artifact pages can be opened against a real delivered document.
 */
async function startResearch(
  page: Page,
  workflowName: string,
  message: string,
): Promise<StartedRun> {
  await page.goto('/')
  return await page.evaluate(
    async ({ workflowName: name, message: text }) => {
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
      const version = workflows.items.find((item) => item.name === name)?.latest_version
      if (!version) throw new Error(`workflow ${name} is missing`)

      const draft = await call<{ id: string }>('/api/v1/requirements', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ workflow_version_id: version.id }),
      })
      await call(`/api/v1/requirements/${draft.id}/messages`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ text }),
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

      const terminal = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'WAITING_USER']
      for (let attempt = 0; attempt < 240; attempt += 1) {
        const snapshot = await call<{
          id: string
          status: string
          completeness: string | null
          artifact_ids: string[]
        }>(`/api/v1/runs/${started.id}`)
        if (terminal.includes(snapshot.status)) {
          return {
            runId: snapshot.id,
            artifactId: snapshot.artifact_ids[0] ?? null,
            status: snapshot.status,
            completeness: snapshot.completeness,
          }
        }
        await new Promise((resolve) => setTimeout(resolve, 250))
      }
      throw new Error('the run did not reach a terminal state')
    },
    { workflowName, message },
  )
}

const START_MESSAGE = '12 人团队想了解公共图书馆夜间开放'

test('opens the delivered report, saves it and reopens the same document', async ({ page }) => {
  const started = await startResearch(page, '公开主题资料研究', START_MESSAGE)
  expect(started.status).toBe('SUCCEEDED')
  expect(started.completeness).toBe('COMPLETE')
  expect(started.artifactId).toBeTruthy()

  await page.goto(`/runs/${started.runId}/artifacts/${started.artifactId}`)
  await expect(page.getByRole('heading', { name: '文档', exact: true })).toBeVisible()
  await expect(page.getByText('完整', { exact: true })).toBeVisible()
  await expect(page.getByText('（COMPLETE）')).toBeVisible()
  await expect(page.getByText(/实际版本/)).toBeVisible()
  // Sources carry both the fetch time and the confirmed publication date.
  expect(await page.locator('.sources li').count()).toBeGreaterThanOrEqual(2)
  await expect(page.getByText(/获取时间 /).first()).toBeVisible()
  // The body is rendered from the delivered Markdown, not raw HTML.
  await expect(page.locator('.safe-markdown')).toContainText('完成程度：COMPLETE')
  await expect(page.locator('.safe-markdown')).toContainText('背景与基本事实')
  await expect(page.locator('.safe-markdown')).toContainText('最新进展')

  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '保存' }).click()
  const file = await download
  expect(file.suggestedFilename()).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]*\.md$/)
  expect(file.suggestedFilename()).not.toContain('/')

  // Saving then reopening shows the same stored document.
  await page.reload()
  await expect(page.getByText('完整', { exact: true })).toBeVisible()
  await expect(page.locator('.safe-markdown')).toContainText('完成程度：COMPLETE')
  await expect(page.getByText('不可访问')).toHaveCount(0)
})

test('a failed read is not shown as delivered and can be fetched again', async ({ page }) => {
  const started = await startResearch(page, '公开主题资料研究', START_MESSAGE)
  expect(started.artifactId).toBeTruthy()

  let failed = false
  await page.route(/\/api\/v1\/artifacts\/[0-9a-f-]+\/content$/, async (route) => {
    if (failed) {
      await route.continue()
      return
    }
    failed = true
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        error: {
          code: 'ARTIFACT_UNAVAILABLE',
          message: 'the stored document failed its integrity check',
          retryable: false,
          details: {},
          request_id: 'e2e',
        },
      }),
    })
  })

  await page.goto(`/runs/${started.runId}/artifacts/${started.artifactId}`)

  await expect(page.getByRole('alert')).toContainText('不可访问')
  await expect(page.getByRole('button', { name: '保存' })).toBeDisabled()
  await expect(page.locator('.safe-markdown')).toHaveCount(0)
  await expect(page.getByText('完成程度：COMPLETE')).toHaveCount(0)

  await page.getByRole('button', { name: '再次获取' }).click()
  await expect(page.getByRole('status')).toContainText('未重新运行研究')
  await expect(page.locator('.safe-markdown')).toContainText('完成程度：COMPLETE')
  await expect(page.getByRole('button', { name: '保存' })).toBeEnabled()
})

test('a deleted document is reported as unavailable, never as delivered', async ({ page }) => {
  const started = await startResearch(page, '公开主题资料研究', START_MESSAGE)
  expect(started.artifactId).toBeTruthy()

  await page.route(/\/api\/v1\/artifacts\/[0-9a-f-]+(\/content)?$/, async (route) => {
    await route.fulfill({
      status: 410,
      contentType: 'application/json',
      body: JSON.stringify({
        error: {
          code: 'ARTIFACT_UNAVAILABLE',
          message: 'the document is deleted or expired',
          retryable: false,
          details: {},
          request_id: 'e2e',
        },
      }),
    })
  })

  await page.goto(`/runs/${started.runId}/artifacts/${started.artifactId}`)
  await expect(page.getByRole('alert')).toContainText('不可访问')
  await expect(page.locator('.safe-markdown')).toHaveCount(0)
  // Nothing is offered for download because the document is not delivered.
  await expect(page.getByRole('button', { name: '保存' })).toHaveCount(0)
  await expect(page.getByText('完成程度：COMPLETE')).toHaveCount(0)
})
