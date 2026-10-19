import { expect, test } from '@playwright/test'

async function openVersions(page: import('@playwright/test').Page): Promise<void> {
  await page.goto('/')
  const card = page.locator('li.card', { hasText: '技术选型对比' })
  await expect(card).toBeVisible()
  await card.getByRole('button', { name: '查看说明' }).click()
  await page.getByRole('button', { name: '版本管理' }).click()
  await expect(page.getByRole('heading', { name: '版本管理' })).toBeVisible()
}

async function versionCount(page: import('@playwright/test').Page): Promise<number> {
  return page.locator('tbody tr').count()
}

test('saves three executable versions, keeps history and isolates a draft', async ({ page }) => {
  await openVersions(page)
  await expect(page.locator('tbody tr')).toHaveCount(1)

  for (const round of [1, 2, 3]) {
    const textarea = page.getByLabel('定义 JSON')
    const original = JSON.parse(await textarea.inputValue()) as Record<string, unknown>
    const updated = { ...original, description: `第 ${round} 次修改后的描述` }
    await textarea.fill(JSON.stringify(updated, null, 2))
    await page.getByLabel('变更说明').fill(`第 ${round} 次保存`)
    await page.getByLabel('确认为可执行版本').check()
    await page.getByRole('button', { name: '保存为新版本' }).click()
    await expect(page.getByRole('status')).toContainText(`v${round + 1}`)
    await expect(page.locator('tbody tr')).toHaveCount(round + 1)
  }

  // A draft is stored but never becomes the latest executable version.
  const draft = JSON.parse(await page.getByLabel('定义 JSON').inputValue()) as Record<string, unknown>
  await page.getByLabel('定义 JSON').fill(
    JSON.stringify({ ...draft, description: '只是草稿，不应成为最新' }, null, 2),
  )
  await page.getByLabel('变更说明').fill('草稿版本')
  await page.getByRole('button', { name: '保存为新版本' }).click()
  await expect(page.getByRole('status')).toContainText('v4')
  await expect(page.locator('tbody tr').first()).toContainText('草稿')
  await expect(page.getByText('最新可执行版本 v3')).toBeVisible()
})

test('a stale editor keeps local content and shows a version conflict', async ({ context, page }) => {
  await openVersions(page)
  await expect(page.locator('tbody tr')).toHaveCount(1)

  const second = await context.newPage()
  await openVersions(second)

  // First editor saves a new version.
  const textarea = page.getByLabel('定义 JSON')
  const original = JSON.parse(await textarea.inputValue()) as Record<string, unknown>
  await textarea.fill(JSON.stringify({ ...original, description: '编辑器 A 的修改' }, null, 2))
  await page.getByLabel('变更说明').fill('A 保存')
  await page.getByLabel('确认为可执行版本').check()
  await page.getByRole('button', { name: '保存为新版本' }).click()
  await expect(page.getByRole('status')).toContainText('v2')

  // Second editor still holds v1 as its baseline.
  const staleText = JSON.stringify({ ...original, description: '编辑器 B 未保存内容' }, null, 2)
  await second.getByLabel('定义 JSON').fill(staleText)
  await second.getByLabel('变更说明').fill('B 的旧基线保存')
  await second.getByRole('button', { name: '保存为新版本' }).click()

  await expect(second.getByRole('alert')).toContainText('工作流版本已变化')
  await expect(second.getByLabel('定义 JSON')).toHaveValue(staleText)
  await second.close()
})

test('rejects invalid JSON locally without calling the server', async ({ page }) => {
  await openVersions(page)
  await page.getByLabel('定义 JSON').fill('{ 这不是 JSON')
  await page.getByLabel('变更说明').fill('非法 JSON')
  await expect(page.getByText('JSON 解析失败')).toBeVisible()
  await expect(page.getByRole('button', { name: '保存为新版本' })).toBeDisabled()
})
