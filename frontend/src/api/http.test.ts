import { describe, expect, it } from 'vitest';
import { ApiError, HttpClient, toApiError } from './http';

const json = (body: unknown, status: number, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });

describe('toApiError', () => {
  it('parses the backend error envelope', async () => {
    const error = await toApiError(
      json(
        {
          error: {
            code: 'TASK_STATE_CONFLICT',
            message: '任务当前状态不允许该操作',
            details: { a: 1 },
            trace_id: 'tr1',
          },
        },
        409,
      ),
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      code: 'TASK_STATE_CONFLICT',
      message: '任务当前状态不允许该操作',
      traceId: 'tr1',
      details: { a: 1 },
    });
  });

  it('falls back to the status line and the trace header for non-envelope bodies', async () => {
    const html = new Response('<html>502</html>', {
      status: 502,
      statusText: 'Bad Gateway',
      headers: { 'x-trace-id': 'hdr' },
    });
    const error = await toApiError(html);
    expect(error.message).toContain('502');
    expect(error.traceId).toBe('hdr');
    expect(error.code).toBe('HTTP_ERROR');
  });

  it('tolerates FastAPI-style and malformed envelopes', async () => {
    expect((await toApiError(json({ detail: 'x' }, 400))).code).toBe('HTTP_ERROR');
    expect((await toApiError(json({ error: 'str' }, 400))).code).toBe('HTTP_ERROR');
    expect((await toApiError(json({ error: { code: 5, message: null } }, 400))).code).toBe(
      'HTTP_ERROR',
    );
  });
});

describe('HttpClient', () => {
  it('sends JSON, no token by default, and returns parsed bodies', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const client = new HttpClient({
      fetchImpl: async (url, init) => {
        calls.push({ url: String(url), init });
        return json({ ok: true }, 200);
      },
    });
    expect(await client.post('/x', { a: 1 })).toEqual({ ok: true });
    expect(calls[0]?.url).toBe('/api/v1/x');
    expect(calls[0]?.init?.body).toBe('{"a":1}');
    const headers = calls[0]?.init?.headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers['Authorization']).toBeUndefined();
  });

  it('never puts a token into the URL', async () => {
    const urls: string[] = [];
    const client = new HttpClient({
      token: () => 'secret-token',
      fetchImpl: async (url) => {
        urls.push(String(url));
        return json({}, 200);
      },
    });
    await client.get('/tasks/t1');
    expect(urls[0]).not.toContain('secret-token');
    expect(client.headers()['Authorization']).toBe('Bearer secret-token');
  });

  it('turns network failures and error statuses into ApiError', async () => {
    const down = new HttpClient({
      fetchImpl: async () => {
        throw new TypeError('fetch failed');
      },
    });
    await expect(down.get('/x')).rejects.toMatchObject({ code: 'NETWORK_ERROR', status: 0 });
    const conflict = new HttpClient({
      fetchImpl: async () =>
        json({ error: { code: 'APPROVAL_MISMATCH', message: 'm', trace_id: 't' } }, 409),
    });
    await expect(conflict.post('/x')).rejects.toMatchObject({
      status: 409,
      code: 'APPROVAL_MISMATCH',
    });
  });

  it('handles 204 and text bodies', async () => {
    const client = new HttpClient({
      fetchImpl: async (url) =>
        String(url).endsWith('/gone')
          ? new Response(null, { status: 204 })
          : new Response('plain', { status: 200, headers: { 'content-type': 'text/plain' } }),
    });
    expect(await client.delete('/gone')).toBeUndefined();
    expect(await client.get('/text')).toBe('plain');
  });
});
