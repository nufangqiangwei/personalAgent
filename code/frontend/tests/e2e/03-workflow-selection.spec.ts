import { expect, test } from '@playwright/test'

test('lists both fixed workflows without any initial input', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '固定流程' })).toBeVisible()
  await expect(page.locator('li.card')).toHaveCount(2)
  await expect(page.getByText('技术选型对比')).toBeVisible()
  await expect(page.getByText('公开主题资料研究').first()).toBeVisible()
  await expect(page.locator('input')).toHaveCount(0)
  // Different applicability is shown before selection.
  await expect(page.getByText('当用户需要比较具体软件产品')).toBeVisible()
  await expect(page.getByText('当用户需要了解一个公开主题的背景')).toBeVisible()
})

test('selects the exact workflow version and can go back', async ({ page }) => {
  let requestedVersionId: string | undefined
  page.on('request', (request) => {
    if (request.url().endsWith('/api/v1/requirements') && request.method() === 'POST') {
      const body = request.postDataJSON() as { workflow_version_id?: string }
      requestedVersionId = body.workflow_version_id
    }
  })

  await page.goto('/')
  const card = page.locator('li.card', { hasText: '公开主题资料研究' })
  await card.getByRole('button', { name: '选择该流程' }).click()

  await expect(page.locator('h1', { hasText: '研究要求' })).toBeVisible()
  await expect(page).toHaveURL(/\/requirements\/[0-9a-f-]{36}/)
  expect(requestedVersionId).toBeTruthy()
  // The draft is bound to the version that was selected in the list.
  await expect(page.getByText('公开主题资料研究').first()).toBeVisible()
  await expect(page.getByText(/版本 v\d+/).first()).toBeVisible()

  // Creating the draft replaces the URL once, so a single back may land on the
  // transient "new" route; keep going until the list is shown again.
  const listHeading = page.getByRole('heading', { name: '固定流程' })
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.goBack()
    if (await listHeading.isVisible().catch(() => false)) break
  }
  await expect(listHeading).toBeVisible()
})

test('shows applicability, directions and the version on the detail page', async ({ page }) => {
  await page.goto('/')
  await page
    .locator('li.card', { hasText: '技术选型对比' })
    .getByRole('button', { name: '查看说明' })
    .click()

  await expect(page.getByRole('heading', { name: '技术选型对比' })).toBeVisible()
  await expect(page.getByText('适用边界')).toBeVisible()
  await expect(page.getByText('官方能力').first()).toBeVisible()
  await expect(page.getByText(/实际使用版本 v\d+/)).toBeVisible()
})

test('a disabled workflow can no longer be selected', async ({ page }) => {
  await page.goto('/')
  const disabled = await page.evaluate(async () => {
    const csrf = document.cookie
      .split(';')
      .map((part) => part.trim())
      .find((part) => part.startsWith('pa_csrf='))
      ?.split('=')[1]
    const list = (await (
      await fetch('/api/v1/workflows', { headers: { accept: 'application/json' } })
    ).json()) as { items: { id: string; revision: number; name: string }[] }
    const target = list.items.find((item) => item.name === '技术选型对比')
    if (!target) return false
    const response = await fetch(`/api/v1/workflows/${target.id}/disable`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-csrf-token': csrf ?? '',
        'idempotency-key': `disable-${Date.now()}`,
        'if-match': `"${target.revision}"`,
      },
      body: JSON.stringify({ reason: 'E2E 停用验证' }),
    })
    return response.ok
  })
  expect(disabled).toBe(true)

  await page.reload()
  await expect(page.locator('li.card')).toHaveCount(1)
  await expect(page.getByText('技术选型对比')).toHaveCount(0)
})
