// Calls to the API (api.py). Errors carry the API's own message (FastAPI's `detail`) when it gives one.
import type { FetchEstimate, PipelineParams, Scrape, Task, TaskOptions } from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(
      path,
      body === undefined
        ? undefined
        : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    )
  } catch {
    throw new ApiError(0, 'Could not reach the API. Is uvicorn running?')
  }
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = data?.detail
    // A 422 from FastAPI lists what was wrong with each field.
    const message = Array.isArray(detail)
      ? detail.map((d: { msg: string }) => d.msg).join('; ')
      : (detail ?? `${res.status} ${res.statusText}`)
    throw new ApiError(res.status, message)
  }
  return data as T
}

export const api = {
  taskOptions: () => request<TaskOptions>('/api/tasks/options'),
  estimate: (params: PipelineParams) => request<FetchEstimate>('/api/tasks/estimate', { kind: 'pipeline', params }),
  start: (params: PipelineParams) => request<Task>('/api/tasks', { kind: 'pipeline', params }),
  abort: (id: number) => request<{ ok: true }>(`/api/tasks/${id}/abort`, {}),
  approve: (id: number, approve: boolean) => request<{ ok: true }>(`/api/tasks/${id}/approval`, { approve }),
  taskLog: (id: number, after: number) => request<{ lines: string[]; next: number }>(`/api/tasks/${id}/log?after=${after}`),
  scrapes: () => request<Scrape[]>('/api/scrapes'),
  task: (id: number) => request<Task>(`/api/tasks/${id}`),
  tasks: (limit = 20) => request<Task[]>(`/api/tasks?limit=${limit}`),
}
