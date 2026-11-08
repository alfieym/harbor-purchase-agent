/** The chat surface: transcript, tool trace, structured cards, composer. */

import { Card } from '../../components/ui/Card'
import { Composer } from './Composer.tsx'
import { ProductCard } from './ProductCard.tsx'
import type { ChatState, ToolActivity } from './types.ts'
import { useChatRun } from './useChatRun.ts'

const STATUS_LABEL: Record<ChatState['status'], string> = {
  idle: '等待提问',
  running: '正在处理',
  finished: '已完成',
  cancelled: '已取消',
  failed: '处理失败',
}

function ToolTrace({ tools }: { tools: ToolActivity[] }) {
  if (tools.length === 0) return null
  return (
    <details className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
      <summary className="cursor-pointer select-none">
        工具调用（{tools.length}）
      </summary>
      <ul className="mt-2 space-y-1">
        {tools.map((tool) => (
          <li key={tool.id} className="font-mono break-all">
            <span className={tool.result === null ? 'text-amber-700' : 'text-emerald-700'}>
              {tool.result === null ? '…' : '✓'}
            </span>{' '}
            {tool.name}
            {tool.args !== '' && <span className="text-slate-400">({tool.args})</span>}
          </li>
        ))}
      </ul>
    </details>
  )
}

export function ChatPanel() {
  const { state, busy, send, cancel, resume } = useChatRun()

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
          采购对话
        </h2>
        <span className="text-xs text-slate-400">
          {STATUS_LABEL[state.status]}
          {state.runId !== null && <span className="ml-2 font-mono">#{state.lastSeq}</span>}
        </span>
      </div>

      <div className="flex min-h-40 flex-col gap-3">
        {state.messages.length === 0 && (
          <p className="text-sm text-slate-500">
            说说你想买什么、预算多少、寄到哪里。回答里的商品卡只来自结构化结果，不解析文本。
          </p>
        )}

        {state.messages.map((message) => (
          <p
            key={message.id}
            className={
              message.role === 'user'
                ? 'ml-8 self-end rounded-lg bg-sky-600 px-3 py-2 text-sm whitespace-pre-wrap text-white'
                : 'mr-8 self-start rounded-lg bg-slate-100 px-3 py-2 text-sm whitespace-pre-wrap text-slate-800'
            }
          >
            {message.content || (message.role === 'assistant' ? '…' : '')}
          </p>
        ))}

        <ToolTrace tools={state.tools} />

        {state.cards.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-2">
            {state.cards.map((card) => (
              <ProductCard key={card.sku_id} card={card} />
            ))}
          </div>
        )}

        {state.error !== null && (
          <div className="flex items-center justify-between gap-3 rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">
            <span>{state.error}</span>
            {state.runId !== null && state.status === 'failed' && (
              <button
                type="button"
                onClick={() => void resume()}
                className="shrink-0 rounded-md border border-rose-300 px-2 py-1 text-xs hover:bg-rose-100"
              >
                重连
              </button>
            )}
          </div>
        )}
      </div>

      <Composer busy={busy} onSend={(text) => void send(text)} onCancel={() => void cancel()} />
    </Card>
  )
}
