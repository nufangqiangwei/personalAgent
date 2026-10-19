import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SafeMarkdown from '@/components/SafeMarkdown.vue'

function render(text: string) {
  return mount(SafeMarkdown, { props: { text } })
}

describe('SafeMarkdown', () => {
  it('renders headings, lists, bold and inline code', () => {
    const wrapper = render('# 标题\n\n- 第一项\n- 第二项\n\n**加粗** 和 `code`')
    expect(wrapper.find('h1').text()).toBe('标题')
    expect(wrapper.findAll('li')).toHaveLength(2)
    expect(wrapper.find('strong').text()).toBe('加粗')
    expect(wrapper.find('code').text()).toBe('code')
  })

  it('never renders raw HTML from the source', () => {
    const wrapper = render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toContain('<script>')
  })

  it('keeps http/https links and blocks dangerous protocols', () => {
    const wrapper = render('[文档](https://example.org/report) [坏的](javascript:alert(1))')
    const link = wrapper.find('a')
    expect(link.attributes('href')).toBe('https://example.org/report')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    expect(wrapper.findAll('a')).toHaveLength(1)
    expect(wrapper.text()).toContain('链接已阻止')
  })

  it('renders fenced code as escaped text', () => {
    const wrapper = render('```\n<b>raw</b>\n```')
    expect(wrapper.find('pre code').text()).toBe('<b>raw</b>')
    expect(wrapper.find('b').exists()).toBe(false)
  })

  it('supports ordered lists and paragraphs', () => {
    const wrapper = render('1. 一\n2. 二\n\n普通段落')
    expect(wrapper.findAll('ol li')).toHaveLength(2)
    expect(wrapper.find('p').text()).toBe('普通段落')
  })
})

describe('ApiClient', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    document.cookie = 'pa_csrf=csrf-token-123; path=/'
  })

  async function loadClient() {
    return import('@/api/client')
  }

  it('sends the CSRF header and an idempotency key on writes', async () => {
    const { ApiClient } = await loadClient()
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ id: 'draft-1' }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const client = new ApiClient({ fetchImpl: fetchMock })
    await client.post('/api/v1/requirements', { workflow_version_id: 'v1' })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/requirements')
    expect(init.method).toBe('POST')
    expect(init.headers['X-CSRF-Token']).toBe('csrf-token-123')
    expect(init.headers['Idempotency-Key']).toBeTruthy()
    expect(JSON.parse(init.body)).toEqual({ workflow_version_id: 'v1' })
  })

  it('generates a distinct key per write and honours a pinned key', async () => {
    const { ApiClient } = await loadClient()
    fetchMock.mockImplementation(
      async () =>
        new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const client = new ApiClient({ fetchImpl: fetchMock })
    await client.post('/a', {})
    await client.post('/a', {})
    await client.post('/a', {}, { idempotencyKey: 'pinned-key' })
    const keys = fetchMock.mock.calls.map((call) => call[1].headers['Idempotency-Key'])
    expect(new Set(keys).size).toBe(3)
    expect(keys[2]).toBe('pinned-key')
  })

  it('sends If-Match from the current ETag', async () => {
    const { ApiClient } = await loadClient()
    fetchMock.mockResolvedValue(
      new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const client = new ApiClient({ fetchImpl: fetchMock })
    await client.post('/a', { value: 1 }, { etag: '"4"' })
    expect(fetchMock.mock.calls[0][1].headers['If-Match']).toBe('"4"')
  })

  it('surfaces 412 as a stale conflict with a user message', async () => {
    const { ApiClient, ApiError, messageFor } = await loadClient()
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: 'CONFIRMATION_STALE',
            message: 'stale',
            retryable: false,
            details: {},
            request_id: 'req-9',
          },
        }),
        { status: 412, headers: { 'content-type': 'application/json' } },
      ),
    )
    const client = new ApiClient({ fetchImpl: fetchMock })
    await expect(client.post('/a', {}, { etag: '"3"' })).rejects.toMatchObject({
      status: 412,
      code: 'CONFIRMATION_STALE',
    })
    expect(messageFor('CONFIRMATION_STALE')).toContain('重新确认')
    expect(ApiError).toBeDefined()
  })

  it('maps a network failure to a retryable NETWORK_ERROR', async () => {
    const { ApiClient } = await loadClient()
    fetchMock.mockRejectedValue(new TypeError('fetch failed'))
    const client = new ApiClient({ fetchImpl: fetchMock })
    await expect(client.get('/health')).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
      retryable: true,
    })
  })

  it('uses a stable message for every catalogued code', async () => {
    const { messageFor } = await loadClient()
    for (const code of ['INPUT_INCOMPLETE', 'MODEL_OUTCOME_UNKNOWN', 'MAINTENANCE_BUSY']) {
      expect(messageFor(code)).not.toContain('undefined')
    }
  })
})
