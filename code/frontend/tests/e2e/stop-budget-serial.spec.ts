import { expect, test } from '@playwright/test'

import { gate, prepareResearch, runStatus, startResearch, waitForTerminal } from './helpers'

async function setPolicy(page: import('@playwright/test').Page, patch: Record<string, number>) {
  await page.goto('/')
  await page.locator('li.card', { hasText: '公开主题资料研究' })
    .getByRole('button', { name: '查看说明' }).click()
  await page.getByRole('button', { name: '版本管理' }).click()
  const definition = JSON.parse(await page.getByLabel('定义 JSON').inputValue())
  const original = { ...definition.execution_policy }
  definition.execution_policy = { ...original, ...patch }
  definition.description += ` 预算验收 ${crypto.randomUUID()}`
  await page.getByLabel('定义 JSON').fill(JSON.stringify(definition, null, 2))
  await page.getByLabel('变更说明').fill('验证真实额度求助控制')
  await page.getByLabel('确认为可执行版本').check()
  await page.getByRole('button', { name: '保存为新版本' }).click()
  await expect(page.getByRole('status')).toContainText('已保存版本')
  return original as Record<string, number>
}

for (const dimension of ['max_model_requests', 'max_tokens', 'max_active_seconds']) {
  test(`budget assistance through the UI: ${dimension}`, async ({ page }) => {
    const original = await setPolicy(page, { [dimension]: 1 })
    try {
      if (dimension === 'max_active_seconds') await gate(page, 'advance-result')
      const runId = await startResearch(page)
      await runStatus(page, 'WAITING_USER')
      const panel = page.locator('article.request', { hasText: 'BUDGET_LIMIT' })
      await expect(panel).toBeVisible()
      await expect(panel).toContainText('已完成步骤')
      await expect(panel).toContainText('剩余步骤')
      await expect(panel).toContainText('已尝试')
      const before = await (await page.request.get(`/api/v1/runs/${runId}`)).json()
      for (let read = 0; read < 3; read += 1) {
        const unchanged = await (await page.request.get(`/api/v1/runs/${runId}`)).json()
        expect(unchanged.status).toBe('WAITING_USER')
        expect(unchanged.budget).toEqual(before.budget)
      }
      await panel.getByRole('combobox').selectOption('DISCUSS')
      await panel.getByLabel('说明', { exact: true }).fill('知道了，先说明问题')
      await panel.getByRole('button', { name: '提交处置' }).click()
      await expect(panel).toContainText('DISCUSS：知道了，先说明问题')
      await runStatus(page, 'WAITING_USER')

      await panel.getByRole('combobox').selectOption('GRANT_AND_CONTINUE')
      const key = { max_model_requests: 'model_requests', max_tokens: 'tokens',
        max_active_seconds: 'active_ms' }[dimension] as string
      await panel.getByLabel('追加额度').fill(JSON.stringify({ [key]: 100000 }))
      await panel.getByLabel('我已知晓费用与账本').check()
      await panel.getByRole('button', { name: '提交处置' }).click()
      await expect(page.getByRole('alert')).toBeVisible()
      await runStatus(page, 'WAITING_USER')
      await panel.getByLabel('明确授权继续执行').check()
      await panel.getByRole('button', { name: '提交处置' }).click()
      await waitForTerminal(page)
      await expect(page).toHaveURL(new RegExp(`/runs/${runId}$`))
      await expect(page.getByRole('link', { name: '查看文档' })).toBeVisible()
      const after = await (await page.request.get(`/api/v1/runs/${runId}`)).json()
      for (const [key, amount] of Object.entries(before.budget.consumed)) {
        expect(after.budget.consumed[key]).toBeGreaterThanOrEqual(Number(amount))
      }
    } finally {
      await gate(page, 'release')
      await setPolicy(page, original)
    }
  })
}

test('stop controls follow the server and a done run offers no pause', async ({ page }) => {
  const runId = await startResearch(page)
  await waitForTerminal(page)

  // The server's allowed_actions, not the UI, decide what is available.
  const pause = page.getByRole('button', { name: '暂停' })
  const cancel = page.getByRole('button', { name: '取消' })
  await expect(pause).toBeDisabled()
  await expect(cancel).toBeDisabled()

  // The same run id is still the only execution in history.
  await page.goto('/runs')
  await expect(page.getByRole('row', { name: new RegExp(runId.slice(0, 8)) })).toBeVisible()
  // Other tests may have created history; this run must appear exactly once.
  await expect(page.getByRole('row', { name: new RegExp(runId.slice(0, 8)) })).toHaveCount(1)
})

test('double start, another task and model chat cannot replace a draining cancelled run', async ({ page, context }) => {
  const second = await context.newPage()
  await prepareResearch(second)
  await prepareResearch(page)
  await gate(page, 'arm-model')
  let starts = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/runs') starts += 1
  })
  try {
    await page.getByRole('button', { name: '开始研究' }).dblclick()
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/)
    expect(starts).toBe(1)
    await expect.poll(async () => (await gate(page, 'status')).active).toBe(1)
    await second.getByRole('button', { name: '开始研究' }).click()
    await expect(second.getByRole('alert')).toBeVisible()
    await expect(second).toHaveURL(/\/requirements\//)

    const chat = await context.newPage()
    await chat.goto('/')
    await chat.locator('li.card', { hasText: '公开主题资料研究' }).getByRole('button', { name: '选择该流程' }).click()
    await chat.getByLabel('补充要求').fill('12 人团队需要进一步分析')
    await chat.getByRole('button', { name: '发送并分析' }).click()
    await expect(chat.getByText('分析状态：WAITING_MODEL')).toBeVisible()
    await expect(chat.getByRole('status')).toContainText('消息已保留')
    await chat.close()

    await page.getByLabel('确认取消').check()
    await page.getByRole('button', { name: '取消', exact: true }).click()
    await runStatus(page, 'CANCELLING')
    await expect(page.getByRole('button', { name: '继续（同 ID）' })).toBeDisabled()
    await gate(page, 'release')
    await runStatus(page, 'CANCELLED')
    expect((await gate(page, 'status')).maximum).toBe(1)
    await page.reload()
    await expect(page.getByRole('button', { name: '继续（同 ID）' })).toBeDisabled()
    await expect(page.getByRole('link', { name: '查看文档' })).toHaveCount(0)
  } finally {
    await gate(page, 'release')
    await second.close()
  }
})

test('an unknown model timeout keeps retry and new research blocked until trusted completion', async ({ page, context }) => {
  await prepareResearch(page)
  const calls = (await gate(page, 'status')).research_calls
  await gate(page, 'timeout-model')
  await page.getByRole('button', { name: '开始研究' }).click()
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/)
  const runId = page.url().split('/').pop() as string
  const chat = await context.newPage()
  try {
    await runStatus(page, 'WAITING_USER')
    await expect(page.locator('article.request')).toContainText('RUNTIME_UNCERTAIN')
    await expect(page.getByRole('button', { name: '继续（同 ID）' })).toBeDisabled()
    await chat.goto('/')
    await chat.locator('li.card', { hasText: '公开主题资料研究' })
      .getByRole('button', { name: '选择该流程' }).click()
    await chat.getByLabel('补充要求').fill('超时后尝试提交另一个研究请求')
    await chat.getByRole('button', { name: '发送并分析' }).click()
    await expect(chat.getByText('分析状态：UNKNOWN')).toBeVisible()
    await expect(chat.getByRole('status')).toContainText('上一次模型结果无法确认')
    expect((await gate(page, 'status')).research_calls).toBe(calls + 1)
    const snapshot = await (await page.request.get(`/api/v1/runs/${runId}`)).json()
    expect(snapshot.status).toBe('WAITING_USER')
  } finally {
    await gate(page, 'finish-unknown')
    await page.getByLabel('确认取消').check()
    await page.getByRole('button', { name: '取消', exact: true }).click()
    await runStatus(page, 'CANCELLED')
    await chat.close()
  }
})

test('switching to a branch cannot replace an in-flight execution', async ({ page, context }) => {
  const parentId = await startResearch(page)
  await waitForTerminal(page)
  await page.goto(`/runs?run=${parentId}`)
  await page.getByLabel('起点节点').selectOption('write_report')
  await page.getByRole('button', { name: '创建分支提案' }).click()
  await page.getByRole('link', { name: '打开派生要求草稿并修改本次差异' }).click()
  await page.getByLabel('我已核对完整摘要，确认按以上要求执行').check()
  await page.getByRole('button', { name: '确认要求' }).click()
  await expect(page.getByText(/已确认：[0-9a-f-]{36}/)).toBeVisible()
  await page.getByRole('link', { name: '返回分支提案并确认起点' }).click()
  await page.getByRole('button', { name: '校验影响范围' }).click()
  await expect(page.getByText('影响范围已确认')).toBeVisible()
  const other = await context.newPage()
  try {
    await prepareResearch(other)
    await gate(page, 'arm-model')
    await other.getByRole('button', { name: '开始研究' }).click()
    await expect(other).toHaveURL(/\/runs\/[0-9a-f-]{36}/)
    await expect.poll(async () => (await gate(page, 'status')).active).toBe(1)
    await page.getByRole('button', { name: /确认新要求与起点/ }).click()
    await expect(page.getByRole('alert')).toBeVisible()
    const history = await (await page.request.get(`/api/v1/runs/${parentId}/history`)).json()
    expect(history.items).toHaveLength(1)
    expect((await gate(page, 'status')).active).toBe(1)
    await gate(page, 'release')
    await waitForTerminal(other)
    const created = page.waitForResponse((response) =>
      /branch-proposals\/[^/]+\/confirm$/.test(response.url()) && response.status() === 201,
    )
    await page.getByRole('button', { name: /确认新要求与起点/ }).click()
    const branch = await (await created).json()
    expect(branch.parent_run_id).toBe(parentId)
    await page.goto(`/runs/${branch.id}`)
    await waitForTerminal(page)
    expect((await gate(page, 'status')).maximum).toBe(1)
  } finally {
    await gate(page, 'release')
    await other.close()
  }
})
