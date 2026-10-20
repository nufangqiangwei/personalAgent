import { expect, test } from '@playwright/test'

import { startResearch, waitForTerminal } from './helpers'

test('setup, data and maintenance pages share one same-origin UI', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/$/)

  await page.goto('/setup')
  await expect(page.getByRole('heading', { name: '首次配置' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '凭据健康' })).toBeVisible()

  await page.goto('/data')
  await expect(page.getByRole('heading', { name: '数据与删除' })).toBeVisible()
  // Deleting needs a selection and an explicit acknowledgement.
  await expect(page.getByRole('button', { name: /删除所选/ })).toBeDisabled()

  await page.goto('/maintenance')
  await expect(page.getByRole('heading', { name: '维护与备份' })).toBeVisible()
  await expect(page.getByText('静默', { exact: true })).toBeVisible()
  // In NORMAL mode a backup is not offered without entering maintenance.
  await expect(page.getByRole('button', { name: '创建备份' })).toBeDisabled()
})

test('enter maintenance, create and verify a backup, then exit', async ({ page }) => {
  await startResearch(page)
  await waitForTerminal(page)

  await page.goto('/maintenance')
  await page.getByLabel('原因').fill('端到端演练')
  await page.getByRole('button', { name: '进入维护' }).click()
  await expect(page.getByText('已请求进入维护')).toBeVisible()
  await expect(page.getByText('MAINTENANCE')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText('是', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '创建备份' }).click()
  await expect(page.getByText('备份已创建并验证')).toBeVisible()
  await expect(page.getByRole('cell', { name: 'VERIFIED' }).first()).toBeVisible()

  // Exiting needs no in-flight action; the run finished before maintenance.
  await page.getByRole('button', { name: '退出维护' }).click()
  await expect(page.getByText('已退出维护')).toBeVisible()
  await expect(page.getByText('NORMAL')).toBeVisible()
})
