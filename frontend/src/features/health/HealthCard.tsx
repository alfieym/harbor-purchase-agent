import { useEffect, useState } from 'react'

import { Card } from '../../components/ui/Card'
import { api } from '../../lib/api/client'
import type { components } from '../../lib/api/schema'

type Health = components['schemas']['HealthResponse']

type State =
  | { status: 'loading' }
  | { status: 'ok'; data: Health }
  | { status: 'error'; message: string }

export function HealthCard() {
  const [state, setState] = useState<State>({ status: 'loading' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setState({ status: 'loading' })

    async function load() {
      try {
        const { data, error, response } = await api.GET('/api/health', {
          signal: controller.signal,
        })
        if (controller.signal.aborted) return

        if (error) {
          setState({ status: 'error', message: `后端返回 ${response.status}` })
          return
        }
        if (!data) {
          setState({
            status: 'error',
            message: `后端返回了空响应（HTTP ${response.status}）`,
          })
          return
        }
        setState({ status: 'ok', data })
      } catch (cause) {
        if (controller.signal.aborted) return
        setState({
          status: 'error',
          message: cause instanceof Error ? cause.message : '无法连接后端',
        })
      }
    }

    void load()
    return () => controller.abort()
  }, [attempt])

  return (
    <Card>
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
          后端健康状态
        </h2>
        <button
          type="button"
          onClick={() => setAttempt((value) => value + 1)}
          className="rounded-md border border-slate-300 px-3 py-1 text-sm text-slate-700 hover:bg-slate-100"
        >
          刷新
        </button>
      </div>

      <div className="mt-4">
        {state.status === 'loading' && (
          <p className="text-slate-500">正在连接 /api/health…</p>
        )}

        {state.status === 'ok' && (
          <div className="space-y-1">
            <p className="flex items-center gap-2 text-lg font-semibold text-emerald-700">
              <span
                className="inline-block size-2 rounded-full bg-emerald-500"
                aria-hidden="true"
              />
              后端正常
            </p>
            <p className="text-sm text-slate-600">
              {state.data.service} · {state.data.version} · {state.data.environment}
            </p>
            <p className="text-sm text-slate-600">
              LLM {state.data.llm_configured ? '已配置' : '未配置'}
            </p>
          </div>
        )}

        {state.status === 'error' && (
          <div className="space-y-2">
            <p className="text-lg font-semibold text-rose-700">连接失败</p>
            <p className="text-sm text-slate-600">{state.message}</p>
            <p className="text-sm text-slate-500">
              请确认后端已启动：
              <code className="rounded bg-slate-100 px-1.5 py-0.5">make dev-api</code>
            </p>
          </div>
        )}
      </div>
    </Card>
  )
}
