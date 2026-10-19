import { expect, test } from '@playwright/test'

async function startFromWorkflow(
  page: import('@playwright/test').Page,
  name: string,
): Promise<void> {
  await page.goto('/')
  const card = page.locator('li.card', { hasText: name })
  await card.getByRole('button', { name: '选择该流程' }).click()
  await expect(page).toHaveURL(/\/requirements\/[0-9a-f-]{36}/)
  await expect(page.locator('h1', { hasText: '研究要求' })).toBeVisible()
}

test('chat collects requirements, accepts a suggestion and confirms explicitly', async ({ page }) => {
  const runRequests: string[] = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/v1/runs')) {
      runRequests.push(request.url())
    }
  })

  await startFromWorkflow(page, '技术选型对比')

  await page.getByLabel('补充要求').fill('我们团队想比较几个 AI 编程工具')
  await page.getByRole('button', { name: '发送并分析' }).click()
  await expect(page.getByText('分析状态：SUCCEEDED')).toBeVisible()

  // The complete H1/H2/H3 summary is rendered from structured data.
  await expect(page.getByRole('heading', { name: '完整摘要' })).toBeVisible()
  await expect(page.getByText('一、研究要求')).toBeVisible()
  await expect(page.getByText('二、优先顺序')).toBeVisible()
  await expect(page.getByText('三、确认与缺口')).toBeVisible()

  // An inferred value stays a pending suggestion until accepted.
  await expect(page.getByRole('heading', { name: '待确认建议' })).toBeVisible()
  await page.getByRole('button', { name: '接受建议' }).first().click()
  await expect(page.getByRole('status')).toContainText('建议已接受')
  await expect(page.getByText('已接受建议')).toBeVisible()

  await page
    .getByLabel('补充要求')
    .fill('12 人团队，预算每人每月 40 元，需要私有化部署')
  await page.getByRole('button', { name: '发送并分析' }).click()
  await expect(page.getByText('必要信息已齐全')).toBeVisible()

  // Confirmation is disabled until the explicit checkbox is set.
  const confirmButton = page.getByRole('button', { name: '确认要求' })
  await expect(confirmButton).toBeDisabled()
  await page.getByLabel('我已核对完整摘要，确认按以上要求执行').check()
  await expect(confirmButton).toBeEnabled()
  await confirmButton.click()

  await expect(page.getByRole('status')).toContainText('已确认')
  await expect(page.getByText(/已确认：[0-9a-f-]{36}/)).toBeVisible()
  // Confirmation alone must not start research.
  expect(runRequests).toEqual([])
})

test('explicit no preference is shown as a value, not a missing field', async ({ page }) => {
  await startFromWorkflow(page, '公开主题资料研究')

  await page.getByLabel('补充要求').fill('这个主题我没有特别偏好')
  await page.getByRole('button', { name: '发送并分析' }).click()
  await expect(page.getByText('分析状态：SUCCEEDED')).toBeVisible()
  await expect(page.getByText('无特别偏好').first()).toBeVisible()
  await expect(page.getByText('优先顺序').first()).toBeVisible()
})

test('an empty message cannot be sent', async ({ page }) => {
  await startFromWorkflow(page, '技术选型对比')
  await expect(page.getByRole('button', { name: '发送并分析' })).toBeDisabled()
})
