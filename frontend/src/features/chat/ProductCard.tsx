/** One product, rendered only from structured state (S14).
 *
 * Every field here came from ``STATE_SNAPSHOT``'s ``CommerceIntentResponse``
 * — never parsed out of the assistant's prose. If the model says something the
 * structured state does not, the card tells the truth and the prose does not.
 */

import type { ProductCard as ProductCardData } from './types.ts'

function price(card: ProductCardData): string {
  try {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: card.currency,
      maximumFractionDigits: 2,
    }).format(card.price_major)
  } catch {
    // An unknown currency code must not take the card down with it.
    return `${card.price_major} ${card.currency}`
  }
}

export function ProductCard({ card }: { card: ProductCardData }) {
  return (
    <article className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold text-slate-900">{card.title}</h3>
          <p className="text-xs text-slate-500">
            {card.brand} · {card.category}
          </p>
        </div>
        <p className="shrink-0 font-semibold text-sky-700">{price(card)}</p>
      </div>

      {card.highlights.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {card.highlights.map((highlight) => (
            <li
              key={highlight}
              className="rounded-full bg-sky-50 px-2 py-0.5 text-xs text-sky-700"
            >
              {highlight}
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-slate-400">
        {card.weight_kg} kg · {card.sku_id}
      </p>
    </article>
  )
}
