import { Card } from './components/ui/Card'
import { HealthCard } from './features/health/HealthCard'

const steps = [
  { label: 'React', note: 'Vite + TypeScript + Tailwind' },
  { label: 'FastAPI', note: '/api 与 OpenAPI 契约' },
  { label: 'AgentScope', note: 'Phase 2：ReAct 采购循环' },
]

function App() {
  return (
    <main className="min-h-screen bg-slate-50 px-4 py-12 text-slate-900 sm:py-16">
      <div className="mx-auto flex max-w-2xl flex-col gap-6">
        <header>
          <p className="text-sm font-medium tracking-wide text-sky-600 uppercase">
            Phase 1 · 仓库骨架
          </p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight sm:text-4xl">
            Harbor 采购 Agent
          </h1>
          <p className="mt-3 text-slate-600">
            「预算 300 元，买个背包寄到中国」——检索商品，给出带价格与币种的 SKU 推荐。
          </p>
        </header>

        <Card>
          <h2 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
            架构链路
          </h2>
          <ol className="mt-4 grid gap-3 sm:grid-cols-3">
            {steps.map((step, index) => (
              <li key={step.label} className="rounded-lg bg-slate-50 p-3">
                <p className="text-xs text-slate-400">0{index + 1}</p>
                <p className="mt-1 font-semibold">{step.label}</p>
                <p className="mt-1 text-xs text-slate-500">{step.note}</p>
              </li>
            ))}
          </ol>
        </Card>

        <HealthCard />

        <footer className="text-sm text-slate-500">
          Agent 对话将在第二阶段接入；本页仅验证前后端连通与配置。
        </footer>
      </div>
    </main>
  )
}

export default App
