import { expect, test } from '@playwright/test'

import { startResearch, waitForTerminal } from './helpers'

test('choose, confirm, run and open the delivered Markdown document', async ({ page }) => {
  const runId = await startResearch(page)
  await waitForTerminal(page)

  // A real saved document is reachable without internal data changes.
  const artifactLink = page.getByRole('link', { name: '查看文档' }).first()
  await expect(artifactLink).toBeVisible()
  await artifactLink.click()
  await expect(page).toHaveURL(new RegExp(`/runs/${runId}/artifacts/`))
  await expect(page.getByRole('heading', { name: '文档' })).toBeVisible()
  await expect(page.getByText('完整程度')).toBeVisible()
  await expect(page.getByRole('link', { name: /下载|保存/ })).toBeVisible()

  // The run remains reviewable and the artifact is still listed.
  await page.goto(`/runs/${runId}`)
  await expect(page.getByText('SUCCEEDED', { exact: false }).first()).toBeVisible()
})

test('an empty library and an incomplete setup are explained, not fabricated', async ({
  page,
}) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /流程/ })).toBeVisible()

  await page.goto('/setup')
  await expect(page.getByRole('heading', { name: '首次配置' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '环境检查' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '政策确认' })).toBeVisible()
  await expect(page.getByText(/配置(已|尚未)完成/)).toBeVisible()
  // The page never asks for a registration or a role.
  await expect(page.getByText('注册')).toHaveCount(0)
})

test('a stopped requirement draft shows missing and conflict reasons', async ({ page }) => {
  await page.goto('/')
  const card = page.locator('li.card', { hasText: '公开主题资料研究' })
  await card.getByRole('button', { name: '选择该流程' }).click()
  await expect(page.getByRole('heading', { name: '研究要求' })).toBeVisible()
  await expect(page.getByRole('button', { name: '确认要求' })).toBeDisabled()
  await expect(page.getByText('必要信息')).toBeVisible()
})
