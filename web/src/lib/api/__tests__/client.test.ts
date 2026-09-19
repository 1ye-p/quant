import { describe, it, expect, vi, afterEach } from 'vitest'
import { request } from '../client'

// ── fetch mock ────────────────────────────────────────────────────────────────

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

afterEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), { status: 200 }),
  )
})

function lastCallHeaders(): Headers {
  return new Headers(fetchMock.mock.calls[0][1].headers as HeadersInit)
}

// ── tests ─────────────────────────────────────────────────────────────────────

describe('request Content-Type handling', () => {
  it('sets JSON Content-Type for regular bodies', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({}), { status: 200 }),
    )
    await request('/foo', { method: 'POST', body: JSON.stringify({ a: 1 }) })
    expect(lastCallHeaders().get('Content-Type')).toBe('application/json')
  })

  it('does NOT set Content-Type for FormData bodies (browser sets multipart boundary)', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({}), { status: 200 }),
    )
    const form = new FormData()
    form.append('file', new File(['a,b\n1,2'], 'ext.csv', { type: 'text/csv' }))
    form.append('config', '{}')

    await request('/datasets/external-indicators/import', {
      method: 'POST',
      body: form as unknown as BodyInit,
    })

    expect(lastCallHeaders().get('Content-Type')).toBeNull()
  })

  it('FormData body is passed through untouched', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({}), { status: 200 }),
    )
    const form = new FormData()
    form.append('file', new File(['a'], 'x.csv'))
    await request('/datasets/external-indicators/preview', {
      method: 'POST',
      body: form as unknown as BodyInit,
    })
    expect(fetchMock.mock.calls[0][1].body).toBe(form)
  })
})
