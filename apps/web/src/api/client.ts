import type {
  ExtractListResponse,
  PredictionListResponse,
  RevisionListResponse,
  ScorecardResponse,
  VarietyDetail,
  VarietyListResponse,
} from '../types/api'
import type { HealthResponse } from '../types/health'

const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export function apiBase(): string {
  return baseUrl
}

export interface FetchResult<T> {
  data: T
  /** true 表示由 Service Worker 缓存兜底（离线/接口不可达），前端应提示「数据可能过期」 */
  fromCache: boolean
}

async function getJsonResult<T>(path: string, signal?: AbortSignal): Promise<FetchResult<T>> {
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return {
    data: (await response.json()) as T,
    fromCache: response.headers.get('x-sw-cache') === '1',
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await getJsonResult<T>(path, signal)).data
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const body = await getJson<HealthResponse>('/ready', signal).catch((e: unknown) => {
    throw e instanceof Error ? e : new Error('健康检查失败')
  })
  if (body === null || typeof body !== 'object' || !('db' in body)) {
    throw new Error('健康检查返回格式异常')
  }
  return body
}

export function fetchPredictions(
  signal?: AbortSignal,
): Promise<FetchResult<PredictionListResponse>> {
  return getJsonResult<PredictionListResponse>('/api/v1/predictions', signal)
}

export function fetchVarietyDetail(
  code: string,
  signal?: AbortSignal,
): Promise<VarietyDetail> {
  return getJson<VarietyDetail>(`/api/v1/varieties/${code}/detail`, signal)
}

export function fetchRevisions(
  sinceId = 0,
  signal?: AbortSignal,
): Promise<RevisionListResponse> {
  return getJson<RevisionListResponse>(`/api/v1/revisions?since_id=${sinceId}`, signal)
}

export function fetchScorecard(days = 60, signal?: AbortSignal): Promise<ScorecardResponse> {
  return getJson<ScorecardResponse>(`/api/v1/scorecard?days=${days}`, signal)
}

export function fetchExtracts(days = 2, signal?: AbortSignal): Promise<ExtractListResponse> {
  return getJson<ExtractListResponse>(`/api/v1/news/extracts?days=${days}&limit=100`, signal)
}

export function fetchVarieties(signal?: AbortSignal): Promise<VarietyListResponse> {
  return getJson<VarietyListResponse>('/api/v1/varieties', signal)
}
