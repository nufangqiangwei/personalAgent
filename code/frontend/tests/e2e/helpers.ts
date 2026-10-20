import { expect, type Page } from '@playwright/test'

/** Drive the full UI path: choose a workflow, chat, confirm, and start research. */
export async function startResearch(
  page: Page,
  workflowName = '公开主题资料研究',
): Promise<string> {
  await prepareResearch(page, workflowName)
  await page.getByRole('button', { name: '开始研究' }).click()
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/)
  return page.url().split('/').pop() as string
}

export async function prepareResearch(page: Page, workflowName = '公开主题资料研究'): Promise<void> {
  await page.goto('/')
  const card = page.locator('li.card', { hasText: workflowName })
  await card.getByRole('button', { name: '选择该流程' }).click()
  await expect(page).toHaveURL(/\/requirements\/[0-9a-f-]{36}/)

  await page
    .getByLabel('补充要求')
    .fill('12 人团队想了解公共图书馆夜间开放')
  await page.getByRole('button', { name: '发送并分析' }).click()
  await expect(page.getByText('分析状态：SUCCEEDED')).toBeVisible()

  // An inferred value stays a pending suggestion until accepted.
  const accept = page.getByRole('button', { name: '接受建议' }).first()
  if ((await accept.count()) > 0) {
    await accept.click()
    await expect(page.getByRole('status')).toContainText('建议已接受')
  }
  // Confirmation needs the explicit checkbox; a pending suggestion also blocks it.
  await page.getByLabel('我已核对完整摘要，确认按以上要求执行').check()
  await expect(page.getByRole('button', { name: '确认要求' })).toBeEnabled({
    timeout: 30_000,
  })
  await page.getByRole('button', { name: '确认要求' }).click()
  await expect(page.getByText(/已确认：[0-9a-f-]{36}/)).toBeVisible()

}

export async function waitForTerminal(page: Page): Promise<void> {
  // Wait for the run status itself, not a step that already succeeded while the
  // final output step is still running.
  await expect(
    page.locator('dd').filter({ hasText: /^SUCCEEDED/ }).first(),
  ).toBeVisible({ timeout: 90_000 })
}

/** Control only the external Mock observation boundary, never business database rows. */
export async function gate(page: Page, command: 'arm' | 'arm-model' | 'advance-result' | 'timeout-model' | 'finish-unknown' | 'fail-model' | 'fault-after-publish' | 'release' | 'status') {
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === 'pa_csrf')
  const response = await page.request.post(`/__test__/gate/${command}`, {
    headers: { 'X-CSRF-Token': csrf?.value ?? '', 'Idempotency-Key': crypto.randomUUID() },
  })
  expect(response.ok()).toBe(true)
  return response.json() as Promise<{ held: boolean; entered: number; model_entered: number; active: number; maximum: number; research_calls: number }>
}

export async function runStatus(page: Page, status: string): Promise<void> {
  await expect(page.locator('dl.summary > dd').first()).toContainText(status)
}
