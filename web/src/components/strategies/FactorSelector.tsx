/**
 * FactorSelector — Multi-select factor picker with category grouping.
 *
 * Fetches factors from GET /factors/available, groups by category,
 * and shows checkboxes with Chinese/English names.
 */
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { factorsApi, type AvailableFactor } from '@/lib/api/factors'
import { FactorHelpPanel } from './FactorHelpPanel'

interface FactorSelectorProps {
  selected: string[]
  onChange: (factors: string[]) => void
}

export function FactorSelector({ selected, onChange }: FactorSelectorProps) {
  const { t } = useTranslation()
  const [helpFactor, setHelpFactor] = useState<AvailableFactor | null>(null)
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')

  const { data, isLoading, error } = useQuery({
    queryKey: ['factors', 'available'],
    queryFn: () => factorsApi.getAvailable(),
    staleTime: 300_000, // 5 minutes
  })

  // Must be called before any early returns (Rules of Hooks)
  const factorMap = useMemo(
    () => new Map((data?.factors ?? []).map(f => [f.name, f])),
    [data?.factors],
  )

  const normalizedSearch = search.trim().toLowerCase()

  const matchesSearch = (factor: AvailableFactor) =>
    !normalizedSearch ||
    factor.name.toLowerCase().includes(normalizedSearch) ||
    (factor.label_zh ?? '').includes(normalizedSearch) ||
    (factor.label_en ?? '').toLowerCase().includes(normalizedSearch)

  /** 组内排序：已选置顶 → 已物化优先 → 名称。 */
  const sortFactors = (factors: AvailableFactor[]) =>
    [...factors].sort((a, b) => {
      const aSel = selected.includes(a.name) ? 0 : 1
      const bSel = selected.includes(b.name) ? 0 : 1
      if (aSel !== bSel) return aSel - bSel
      const aMat = a.status === 'materialized' ? 0 : 1
      const bMat = b.status === 'materialized' ? 0 : 1
      if (aMat !== bMat) return aMat - bMat
      return a.name.localeCompare(b.name)
    })

  const selectedFactors = useMemo(
    () =>
      selected
        .map(name => factorMap.get(name))
        .filter((f): f is AvailableFactor => f != null)
        .filter(matchesSearch),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selected, factorMap, normalizedSearch],
  )

  const toggleCategory = (category: string) => {
    setExpandedCategories(prev => {
      const next = new Set(prev)
      if (next.has(category)) {
        next.delete(category)
      } else {
        next.add(category)
      }
      return next
    })
  }

  const toggleFactor = (factorName: string) => {
    onChange(
      selected.includes(factorName)
        ? selected.filter(f => f !== factorName)
        : [...selected, factorName]
    )
  }

  const toggleAllInCategory = (factorNames: string[]) => {
    const allSelected = factorNames.every(f => selected.includes(f))
    if (allSelected) {
      onChange(selected.filter(f => !factorNames.includes(f)))
    } else {
      const newSelected = [...selected]
      factorNames.forEach(f => {
        if (!newSelected.includes(f)) {
          newSelected.push(f)
        }
      })
      onChange(newSelected)
    }
  }

  if (isLoading) {
    return (
      <div className="border rounded-lg p-4 bg-gray-50">
        <div className="text-sm text-gray-500">{t('component.strategies.factor_selector.loading')}</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="border rounded-lg p-4 bg-red-50">
        <div className="text-sm text-red-600">{t('component.strategies.factor_selector.load_failed', { message: error.message })}</div>
      </div>
    )
  }

  if (!data) return null

  const renderFactorRow = (factor: AvailableFactor) => (
    <div key={factor.name} className="flex items-center px-4 py-2 hover:bg-blue-50">
      <label className="flex items-center gap-3 flex-1 cursor-pointer">
        <input
          type="checkbox"
          checked={selected.includes(factor.name)}
          onChange={() => toggleFactor(factor.name)}
          className="rounded"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-700">
              {factor.label_zh}
            </span>
            <span className="text-xs text-gray-400 font-mono">
              {factor.name}
            </span>
            {factor.status === 'materialized' && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700">
                {t('component.strategies.factor_selector.status_materialized')}
              </span>
            )}
            {factor.is_custom && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">
                {t('component.strategies.factor_selector.status_custom')}
              </span>
            )}
          </div>
        </div>
      </label>
      <button
        className="text-xs text-gray-400 hover:text-blue-600 ml-2"
        onClick={() => setHelpFactor(factor)}
        title={t('component.strategies.factor_selector.view_details')}
        aria-label={t('component.strategies.factor_selector.view_factor_details')}
      >
        ⓘ
      </button>
    </div>
  )

  const filteredCategories = data.categories
    .map(category => ({
      ...category,
      factors: category.factors.filter(name => {
        const f = factorMap.get(name)
        return f != null && matchesSearch(f)
      }),
    }))
    .filter(category => category.factors.length > 0)

  return (
    <div className="space-y-3">
      {/* Search box */}
      <input
        type="text"
        className="input w-full"
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder={t('component.strategies.factor_selector.search_placeholder')}
      />

      <div className="border rounded-lg divide-y">
        {/* Pinned selected factors (top) */}
        {selectedFactors.length > 0 && (
          <div>
            <div className="px-3 py-2 bg-blue-50">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-blue-800">
                  {t('component.strategies.factor_selector.selected_group')}
                </span>
                <span className="text-xs text-blue-500">
                  ({selectedFactors.length})
                </span>
                <button
                  className="text-xs text-blue-600 hover:text-blue-800 ml-auto px-2 py-1"
                  onClick={() => onChange(selected.filter(f => !factorMap.has(f) || !matchesSearch(factorMap.get(f)!)))}
                >
                  {t('component.strategies.factor_selector.deselect_all')}
                </button>
              </div>
            </div>
            <div className="divide-y">
              {selectedFactors.map(renderFactorRow)}
            </div>
          </div>
        )}

        {filteredCategories.map(category => {
          const isExpanded = expandedCategories.has(category.name)
          const categoryFactors = sortFactors(
            category.factors
              .map(name => factorMap.get(name))
              .filter((f): f is AvailableFactor => f != null)
          )
          const selectedInCategory = categoryFactors.filter(f => selected.includes(f.name))

          return (
            <div key={category.name}>
              {/* Category header */}
              <div className="flex items-center justify-between px-3 py-2 bg-gray-50 hover:bg-gray-100 cursor-pointer"
                role="button"
                tabIndex={0}
                onClick={() => toggleCategory(category.name)}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCategory(category.name) } }}>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-400">
                    {isExpanded ? '▼' : '▶'}
                  </span>
                  <span className="text-sm font-medium text-gray-700">
                    {category.label_zh}
                  </span>
                  <span className="text-xs text-gray-400">
                    {category.label_en}
                  </span>
                  <span className="text-xs text-gray-400">
                    ({selectedInCategory.length}/{categoryFactors.length})
                  </span>
                </div>
                <button
                  className="text-xs text-blue-600 hover:text-blue-800 px-2 py-1"
                  onClick={(e) => {
                    e.stopPropagation()
                    toggleAllInCategory(category.factors)
                  }}
                >
                  {selectedInCategory.length === categoryFactors.length ? t('component.strategies.factor_selector.deselect_all') : t('component.strategies.factor_selector.select_all')}
                </button>
              </div>

              {/* Factor list */}
              {isExpanded && (
                <div className="divide-y">
                  {categoryFactors.map(renderFactorRow)}
                </div>
              )}
            </div>
          )
        })}

        {filteredCategories.length === 0 && selectedFactors.length === 0 && (
          <div className="px-4 py-6 text-center text-sm text-gray-400">
            {t('component.strategies.factor_selector.no_matches')}
          </div>
        )}
      </div>

      {/* Help panel */}
      {helpFactor && (
        <FactorHelpPanel
          factor={helpFactor}
          onClose={() => setHelpFactor(null)}
        />
      )}
    </div>
  )
}
