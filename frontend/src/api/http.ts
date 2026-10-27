/** 统一的请求封装：解析后端的 `{error:{code,message,details,trace_id}}` 信封。 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly traceId: string;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    traceId = '',
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
    this.traceId = traceId;
  }
}

interface ErrorEnvelope {
  error?: {
    code?: unknown;
    message?: unknown;
    details?: unknown;
    trace_id?: unknown;
  };
}

/** 把任意失败响应转成 ApiError；响应体不是预期信封（代理页、HTML、空）时回退到状态行。 */
export async function toApiError(response: Response): Promise<ApiError> {
  const headerTrace = response.headers.get('x-trace-id') ?? '';
  let parsed: ErrorEnvelope | undefined;
  try {
    parsed = (await response.json()) as ErrorEnvelope;
  } catch {
    parsed = undefined;
  }
  const error = parsed?.error;
  if (error && typeof error === 'object') {
    const details =
      error.details && typeof error.details === 'object'
        ? (error.details as Record<string, unknown>)
        : {};
    return new ApiError(
      response.status,
      typeof error.code === 'string' ? error.code : 'HTTP_ERROR',
      typeof error.message === 'string' ? error.message : response.statusText,
      details,
      typeof error.trace_id === 'string' ? error.trace_id : headerTrace,
    );
  }
  return new ApiError(
    response.status,
    'HTTP_ERROR',
    `请求失败（${response.status} ${response.statusText || '无响应说明'}）`,
    {},
    headerTrace,
  );
}

export type Fetch = typeof fetch;

export interface HttpOptions {
  baseUrl?: string;
  fetchImpl?: Fetch;
  /** 不走同源代理时才需要；缺省不带令牌（令牌由代理在服务端侧注入）。 */
  token?: () => string | undefined;
}

export class HttpClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: Fetch;
  private readonly token: () => string | undefined;

  constructor(options: HttpOptions = {}) {
    this.baseUrl = options.baseUrl ?? '/api/v1';
    this.fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
    this.token = options.token ?? (() => undefined);
  }

  headers(extra: Record<string, string> = {}): Record<string, string> {
    const token = this.token();
    return { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra };
  }

  url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    let response: Response;
    try {
      response = await this.fetchImpl(this.url(path), {
        method,
        headers: this.headers(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new ApiError(0, 'NETWORK_ERROR', '无法连接后端，请确认服务已启动', {
        cause: String(cause),
      });
    }
    if (!response.ok) throw await toApiError(response);
    if (response.status === 204) return undefined as T;
    const type = response.headers.get('content-type') ?? '';
    return (type.includes('json') ? await response.json() : await response.text()) as T;
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>('GET', path);
  }
  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('POST', path, body ?? {});
  }
  put<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>('PUT', path, body);
  }
  patch<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>('PATCH', path, body);
  }
  delete<T>(path: string): Promise<T> {
    return this.request<T>('DELETE', path);
  }
}

export const http = new HttpClient();
