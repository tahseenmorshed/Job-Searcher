// Follows the latest fetch (a task on the API, see tasks.py) while it runs.
import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'
import type { PipelineParams, Task } from './types'

const POLL_MS = 1500

export function active(task: Task): boolean {
  return task.status === 'queued' || task.status === 'running' || task.status === 'waiting'
}

// The latest fetch, polled while it runs. onFinished is called once when it stops, so the
// page can reload its jobs and spend.
export function useFetchTask(onFinished: () => void) {
  const [task, setTask] = useState<Task | null>(null)
  const finished = useRef(onFinished)
  useEffect(() => {
    finished.current = onFinished
  })

  // When the page opens, pick up a fetch that's still going (started before a reload, say).
  useEffect(() => {
    api
      .tasks(5)
      .then((tasks) => {
        const latest = tasks.find((t) => t.kind === 'pipeline')
        if (latest && active(latest)) setTask(latest)
      })
      .catch(() => {})
  }, [])

  const running = task !== null && active(task)
  const id = task?.id
  useEffect(() => {
    if (!running || id === undefined) return
    const timer = setInterval(() => {
      api
        .task(id)
        .then((t) => {
          setTask(t)
          if (!active(t)) finished.current()
        })
        .catch(() => {}) // a missed poll is retried on the next tick
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [running, id])

  async function start(params: PipelineParams) {
    try {
      setTask(await api.start(params))
    } catch (e) {
      // Already running (started in another tab, say): follow that one instead.
      if (e instanceof ApiError && e.status === 409) {
        const latest = (await api.tasks(5)).find((t) => t.kind === 'pipeline' && active(t))
        if (latest) return setTask(latest)
      }
      throw e
    }
  }

  // Continue or stop the step waiting for approval; the next poll shows the result.
  async function answer(approve: boolean) {
    if (!task) return
    await api.approve(task.id, approve)
    setTask(await api.task(task.id))
  }

  // Stop after the current job; what's done so far is kept.
  async function abort() {
    if (!task) return
    await api.abort(task.id)
    setTask(await api.task(task.id))
  }

  return { task, running, start, answer, abort, dismiss: () => setTask(null) }
}

export type FetchTask = ReturnType<typeof useFetchTask>
