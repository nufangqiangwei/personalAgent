import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ArtifactPage from '@/pages/ArtifactPage.vue'

const metadata = {
  id: 'artifact-1',
  run_id: 'run-1',
  workflow_version_id: 'version-7',
  kind: 'document',
  format: 'markdown',
  structure: 'report',
  completeness: 'COMPLETE',
  manifest_hash: 'sha256:manifest',
  created_at: '2026-10-19T12:00:10Z',
  access_state: 'AVAILABLE',
  size_bytes: 2048,
  sha256: 'a'.repeat(64),
  document: {
    schema_version: 1,
    title: '公开主题资料研究',
    structure: 'report',
    completeness: 'COMPLETE',
    objective: '图书馆夜间开放',
    constraints: [],
    directions: [
      { id: 'background', title: '背景与基本事实', status: 'covered', gaps: [], findings: [] },
      { id: 'latest', title: '最新进展', status: 'covered', gaps: [], findings: [] },
    ],
    qualified: [],
    unknown_candidates: [],
    rejected_candidates: [],
    sources: [
      {
        source_id: 'src-1',
        url: 'https://example.org/library/night-opening',
        title: '公共图书馆夜间开放试点扩大',
        fetched_at: '2026-10-19T12:00:01Z',
        published_at: '2026-03-02',
      },
      {
        source_id: 'src-2',
        url: 'https://example.org/library/latest',
        title: '2026 年新增夜间开放城市',
        fetched_at: '2026-10-19T12:00:02Z',
        published_at: null,
      },
      {
        source_id: 'src-3',
        url: 'javascript:alert(1)',
        title: '危险链接',
        fetched_at: '2026-10-19T12:00:03Z',
        published_at: null,
      },
    ],
    gaps: [],
    unverified: [],
    notes: [],
  },
}

const markdown = '# 公开主题资料研究\n\n完成程度：COMPLETE\n\n## 背景与基本事实\n'

type Handler = (url: string) => Response

function installFetch(handler: Handler) {
  const mock = vi.fn(async (input: RequestInfo | URL) => handler(String(input)))
  vi.stubGlobal('fetch', mock)
  return mock
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function errorResponse(code: string, status: number): Response {
  return jsonResponse(
    { error: { code, message: code, retryable: false, details: {}, request_id: 'r1' } },
    status,
  )
}

function mountPage() {
  return mount(ArtifactPage, {
    props: { id: 'run-1', artifactId: 'artifact-1' },
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ArtifactPage', () => {
  it('reads the delivered document with completeness, version, sources and gaps', async () => {
    installFetch((url) =>
      url.endsWith('/content')
        ? new Response(markdown, {
            status: 200,
            headers: { 'content-type': 'text/markdown; charset=utf-8' },
          })
        : jsonResponse(metadata),
    )
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('完整')
    expect(wrapper.text()).toContain('version-7')
    expect(wrapper.text()).toContain('背景与基本事实')
    expect(wrapper.text()).toContain('公共图书馆夜间开放试点扩大')
    expect(wrapper.text()).toContain('发布时间 2026-03-02')
    // A missing publication date is unknown; the fetch time never stands in for it.
    expect(wrapper.text()).toContain('发布时间未注明')
    expect(wrapper.text()).toContain('获取时间 2026-10-19T12:00:01Z')
    // Unsafe links never become anchors.
    const hrefs = wrapper.findAll('a').map((anchor) => anchor.attributes('href'))
    expect(hrefs.every((href) => href === undefined || href.startsWith('https://'))).toBe(true)
    expect(wrapper.text()).toContain('链接已阻止')
    // The body is rendered from the Markdown document.
    expect(wrapper.text()).toContain('公开主题资料研究')
  })

  it('labels a partial document as partial and lists its gaps', async () => {
    const partial = {
      ...metadata,
      completeness: 'PARTIAL',
      document: {
        ...metadata.document,
        completeness: 'PARTIAL',
        directions: [
          { id: 'background', title: '背景与基本事实', status: 'covered', gaps: [], findings: [] },
          { id: 'latest', title: '最新进展', status: 'missing', gaps: ['缺少最新进展的公开证据'] },
        ],
        gaps: ['缺少最新进展的公开证据'],
      },
    }
    installFetch((url) =>
      url.endsWith('/content')
        ? new Response('# 报告\n\n完成程度：PARTIAL\n', {
            status: 200,
            headers: { 'content-type': 'text/markdown' },
          })
        : jsonResponse(partial),
    )
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('部分完成')
    expect(wrapper.text()).toContain('PARTIAL')
    expect(wrapper.text()).toContain('本次为部分交付')
    expect(wrapper.text()).toContain('缺少最新进展的公开证据')
    expect(wrapper.text()).toContain('缺少该方向的结果')
  })

  it('never presents an unavailable document as delivered', async () => {
    installFetch((url) => {
      if (url.endsWith('/content')) return errorResponse('ARTIFACT_UNAVAILABLE', 410)
      return jsonResponse({ ...metadata, access_state: 'EXPIRED', completeness: 'PARTIAL' })
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('不可访问')
    expect(wrapper.text()).not.toContain('完成程度：COMPLETE')
    const save = wrapper.findAll('button').find((button) => button.text() === '保存')
    expect(save?.attributes('disabled')).toBeDefined()
  })

  it('re-reads the same document without re-running research', async () => {
    const fetchMock = installFetch((url) =>
      url.endsWith('/content')
        ? new Response(markdown, { status: 200, headers: { 'content-type': 'text/markdown' } })
        : jsonResponse(metadata),
    )
    const wrapper = mountPage()
    await flushPromises()

    const before = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/content'))
    expect(before).toHaveLength(1)

    const reread = wrapper.findAll('button').find((button) => button.text() === '再次获取')
    await reread?.trigger('click')
    await flushPromises()

    const after = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/content'))
    expect(after).toHaveLength(2)
    // Only reads are issued: no research start endpoint is called.
    expect(fetchMock.mock.calls.every(([input]) => String(input).startsWith('/api/v1/artifacts/'))).toBe(
      true,
    )
    expect(wrapper.text()).toContain('未重新运行研究')
    expect(wrapper.text()).toContain('公开主题资料研究')
  })

  it('saves the document under a safe filename', async () => {
    installFetch((url) =>
      url.endsWith('/content')
        ? new Response(markdown, { status: 200, headers: { 'content-type': 'text/markdown' } })
        : jsonResponse(metadata),
    )
    const createObjectURL = vi.fn(() => 'blob:document')
    const revokeObjectURL = vi.fn()
    Object.assign(URL, { createObjectURL, revokeObjectURL })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    const wrapper = mountPage()
    await flushPromises()

    const save = wrapper.findAll('button').find((button) => button.text() === '保存')
    await save?.trigger('click')

    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
    const anchor = click.mock.instances[0] as unknown as HTMLAnchorElement
    expect(anchor.download).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]*\.md$/)
    expect(anchor.download).not.toContain('/')
    expect(anchor.download).not.toContain('\\')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:document')
  })
})
