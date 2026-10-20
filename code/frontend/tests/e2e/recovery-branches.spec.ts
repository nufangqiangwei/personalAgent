import { expect, test } from '@playwright/test'

import { gate, prepareResearch, runStatus, startResearch, waitForTerminal } from './helpers'

for (const scenario of [
  { name: 'only the selected output changes', text: 'P8-output：本次输出改为清单', ready: true, move: false },
  { name: 'an earlier input changes and the user confirms moving', text: 'P8-topic：主题改为城市夜间公交', ready: false, move: true },
  { name: 'the user refuses to move the start', text: 'P8-topic：主题改为城市夜间公交', ready: false, move: false },
  { name: 'the input impact cannot be established', text: 'P8-unknown：尚不明确的要求', ready: false, move: false },
]) {
  test(`branch impact: ${scenario.name}`, async ({ page }) => {
    const runId = await startResearch(page)
    await waitForTerminal(page)
    await page.goto(`/runs?run=${runId}`)
    await page.getByLabel('起点节点').selectOption('write_report')
    await page.getByRole('button', { name: '创建分支提案' }).click()
    await page.getByRole('link', { name: '打开派生要求草稿并修改本次差异' }).click()
    await page.getByLabel('补充要求').fill(scenario.text)
    await page.getByRole('button', { name: '发送并分析' }).click()
    await expect(page.getByText('分析状态：SUCCEEDED')).toBeVisible()
    await page.getByRole('link', { name: '返回分支提案并确认起点' }).click()
    await page.getByRole('button', { name: '校验影响范围' }).click()
    await expect(page.getByText(scenario.ready ? '影响范围已确认' : '当前起点被阻塞')).toBeVisible()
    if (!scenario.ready) {
      await expect(page.getByRole('button', { name: /确认新要求与起点/ })).toBeDisabled()
      await expect(page.getByLabel('起点节点')).toHaveValue('write_report')
    }
    if (scenario.move) {
      await page.getByLabel('起点节点').selectOption('collect_background')
      await page.getByRole('button', { name: '校验影响范围' }).click()
      await expect(page.getByText('影响范围已确认')).toBeVisible()
    }
    const history = await (await page.request.get(`/api/v1/runs/${runId}/history`)).json()
    expect(history.items).toHaveLength(1) // Neither validation nor rejecting a move starts a run.
  })
}

test('two explicit resumes keep the run id and append attempts without replacing history', async ({ page }) => {
  await page.goto('/')
  await gate(page, 'arm')
  const runId = await startResearch(page)
  try {
    for (let cycle = 0; cycle < 2; cycle += 1) {
      await expect.poll(async () => (await gate(page, 'status')).entered).toBe(cycle + 1)
      await page.getByRole('button', { name: '暂停', exact: true }).click()
      await runStatus(page, 'PAUSING')
      await expect(page.getByText(/正在收敛：/)).toBeVisible()
      await gate(page, 'release')
      await runStatus(page, 'PAUSED')
      if (cycle === 0) await gate(page, 'arm')
      await page.getByRole('button', { name: '继续（同 ID）' }).click()
      await expect(page).toHaveURL(new RegExp(`/runs/${runId}$`))
    }
    await waitForTerminal(page)
    await expect(page.getByRole('link', { name: '查看文档' })).toBeVisible()
    const response = await page.request.get(`/api/v1/runs/${runId}/steps`)
    const steps = (await response.json()).items as { attempts: unknown[] }[]
    expect(steps.every((step) => step.attempts.length <= 1)).toBe(true)
    const events = await (await page.request.get(`/api/v1/runs/${runId}/event-history`)).json()
    expect(events.items.filter((frame: { event: string }) => frame.event === 'recovery.checked')).toHaveLength(2)
  } finally {
    await gate(page, 'release')
  }
})

for (const startStep of ['collect_background', 'merge_findings', 'write_report']) {
test(`history preserves proposals and forks twice from ${startStep}`, async ({
  page,
}) => {
  let runId = await startResearch(page)
  const rootId = runId
  await waitForTerminal(page)

  for (let fork = 0; fork < 2; fork += 1) {
  await page.goto(`/runs?run=${runId}`)
  await expect(page.getByRole('heading', { name: '执行历史与分支' })).toBeVisible()
  const row = page.getByRole('row', { name: new RegExp(runId.slice(0, 8)) })
  await expect(row).toBeVisible()
  if (fork === 0) await expect(row.getByText('根执行')).toBeVisible()

  // Select the run and create a proposal for a step without a requirement change.
  await expect(page.getByRole('heading', { name: '根与分支' })).toBeVisible()
  await expect(page.getByText('不需要操作 Git')).toBeVisible()

  await page.getByLabel('起点节点').selectOption(startStep)
  await page.getByRole('button', { name: '创建分支提案' }).click()
  await expect(page.getByText('提案已创建')).toBeVisible()
  await expect(page.getByRole('link', { name: '打开派生要求草稿并修改本次差异' })).toBeVisible()

  await page.getByRole('link', { name: '打开派生要求草稿并修改本次差异' }).click()
  await expect(page.getByRole('heading', { name: '完整摘要', exact: true })).toBeVisible()
  await page.getByLabel('我已核对完整摘要，确认按以上要求执行').check()
  await page.getByRole('button', { name: '确认要求' }).click()
  await expect(page.getByText(/已确认：[0-9a-f-]{36}/)).toBeVisible()
  await expect(page.getByRole('button', { name: '开始研究' })).toHaveCount(0)
  await page.getByRole('link', { name: '返回分支提案并确认起点' }).click()
  await page.reload()
  await page.getByRole('button', { name: '校验影响范围' }).click()
  await expect(page.getByText('影响范围已确认')).toBeVisible()
  await expect(page.getByRole('heading', { name: '核验报告' })).toBeVisible()
  const created = page.waitForResponse((response) =>
    /branch-proposals\/[^/]+\/confirm$/.test(response.url()) && response.status() === 201,
  )
  await page.getByRole('button', { name: /确认新要求与起点/ }).click()
  const branch = await (await created).json()
  expect(branch.id).not.toBe(runId)
  expect(branch.parent_run_id).toBe(runId)
  expect(branch.root_run_id).toBe(rootId)
  expect(branch.fork_step_id).toBe(startStep)
  await page.goto(`/runs/${branch.id}`)
  await waitForTerminal(page)
  await expect(page.getByRole('link', { name: '查看文档' })).toBeVisible()
  runId = branch.id
  }
})
}

test('a completed run exposes no pause action and shows its trusted events', async ({
  page,
}) => {
  const runId = await startResearch(page)
  await waitForTerminal(page)

  await expect(page.getByRole('button', { name: '暂停' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '取消' })).toBeDisabled()
  await expect(page.getByRole('heading', { name: '事件' })).toBeVisible()
  await expect(page.getByText(/迟到事件/)).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '求助与处置' })).toBeVisible()
  await expect(page.getByText('当前没有待处理的求助')).toBeVisible()

  // The run id is stable in the URL and history remains reachable.
  await expect(page.getByRole('link', { name: '查看执行历史与分支' })).toBeVisible()
  expect(page.url()).toContain(runId)
})

test('missing prerequisites stay blocked after an explicitly accepted failure report', async ({ page }) => {
  await prepareResearch(page, '技术选型对比')
  await gate(page, 'fail-model')
  try {
    await page.getByRole('button', { name: '开始研究' }).click()
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/)
    const runId = page.url().split('/').pop() as string
    await runStatus(page, 'WAITING_USER')
    const review = page.locator('article.request', { hasText: 'HUMAN_REVIEW' })
    await review.getByRole('combobox').selectOption('GRANT_AND_CONTINUE')
    await review.getByLabel('我已作答并授权继续').check()
    await gate(page, 'release')
    await review.getByRole('button', { name: '提交处置' }).click()
    await waitForTerminal(page)
    await page.goto(`/runs?run=${runId}`)
    await page.getByLabel('起点节点').selectOption('write_comparison')
    await page.getByRole('button', { name: '创建分支提案' }).click()
    await page.getByRole('button', { name: '校验影响范围' }).click()
    await expect(page.getByText('当前起点被阻塞')).toBeVisible()
    await expect(page.getByText(/起点前置步骤尚未成功/)).toBeVisible()
    await expect(page.getByRole('button', { name: /确认新要求与起点/ })).toBeDisabled()
  } finally {
    await gate(page, 'release')
  }
})

test('a published file must be verified before a branch can proceed', async ({ page }) => {
  await page.goto('/')
  await gate(page, 'fault-after-publish')
  const runId = await startResearch(page)
  await runStatus(page, 'FAILED')
  await page.goto(`/runs?run=${runId}`)
  await expect(page.getByRole('heading', { name: '文件写入核验' })).toBeVisible()
  await expect(page.getByText('INTENT', { exact: true })).toBeVisible()
  await page.getByLabel('起点节点').selectOption('write_report')
  await page.getByRole('button', { name: '创建分支提案' }).click()
  await page.getByRole('link', { name: '打开派生要求草稿并修改本次差异' }).click()
  await page.getByLabel('我已核对完整摘要，确认按以上要求执行').check()
  await page.getByRole('button', { name: '确认要求' }).click()
  await expect(page.getByText(/已确认：[0-9a-f-]{36}/)).toBeVisible()
  await page.getByRole('link', { name: '返回分支提案并确认起点' }).click()
  await page.getByRole('button', { name: '校验影响范围' }).click()
  await page.getByRole('button', { name: /确认新要求与起点/ }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  expect((await (await page.request.get(`/api/v1/runs/${runId}/history`)).json()).items).toHaveLength(1)
  await page.getByRole('button', { name: '核验实际写入' }).click()
  await expect(page.getByText('APPLIED', { exact: true })).toBeVisible()
  const created = page.waitForResponse((response) =>
    /branch-proposals\/[^/]+\/confirm$/.test(response.url()) && response.status() === 201,
  )
  await page.getByRole('button', { name: /确认新要求与起点/ }).click()
  const branch = await (await created).json()
  await page.goto(`/runs/${branch.id}`)
  await waitForTerminal(page)
  const oldRun = await (await page.request.get(`/api/v1/runs/${runId}`)).json()
  expect(oldRun.status).toBe('FAILED')
})
