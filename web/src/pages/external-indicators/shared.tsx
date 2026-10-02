/**
 * Shared building blocks for the external-indicators tabs (split out of
 * ExternalIndicatorsPage.tsx in P3-5 to keep the page file lean).
 */

import type { ReactNode } from 'react'

/** Modal shell (Tailwind, same overlay style as ConfirmDialog). */
export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl p-6 max-w-md w-full mx-4 max-h-[85vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold text-gray-900 mb-4">{title}</h3>
        {children}
      </div>
    </div>
  )
}

export function statusBadgeClass(status: string | null): string {
  if (status === 'ok') return 'bg-green-100 text-green-700'
  if (status === 'error') return 'bg-red-100 text-red-700'
  if (status === 'running') return 'bg-blue-100 text-blue-700 animate-pulse'
  return 'bg-gray-100 text-gray-600'
}
