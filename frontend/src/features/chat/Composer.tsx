/** The ask box. Enter sends, Shift+Enter breaks the line. */

import { useState } from 'react'

type ComposerProps = {
  busy: boolean
  onSend: (text: string) => void
  onCancel: () => void
}

export function Composer({ busy, onSend, onCancel }: ComposerProps) {
  const [text, setText] = useState('')

  function submit() {
    const content = text.trim()
    if (content === '' || busy) return
    setText('')
    onSend(content)
  }

  return (
    <form
      className="flex items-end gap-2 border-t border-slate-200 pt-4"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      <label className="sr-only" htmlFor="composer">
        描述你想买的东西
      </label>
      <textarea
        id="composer"
        value={text}
        rows={2}
        placeholder="预算 300 元，买个背包寄到中国"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            submit()
          }
        }}
        className="min-h-10 flex-1 resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
      />
      {busy ? (
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg border border-rose-300 px-4 py-2 text-sm font-medium text-rose-700 hover:bg-rose-50"
        >
          停止
        </button>
      ) : (
        <button
          type="submit"
          disabled={text.trim() === ''}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          发送
        </button>
      )}
    </form>
  )
}
