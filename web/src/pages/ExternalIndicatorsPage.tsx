/**
 * External indicator management page (Phase 1 T6).
 *
 * Tab shell only — tab bodies live in ./external-indicators/:
 *  1. Catalog list (stale badge, edit via PATCH whitelist, delete with
 *     purge-data dual semantics)
 *  2. CSV import wizard (existing ExternalIndicatorsImportPage, embedded)
 *  3. Built-in catalog (P2-4b: enable-with-backfill, per-key refresh, runs)
 *  4. Custom sources (P3-5: custom_http config form + connection test)
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { queryKeys } from '@/lib/queryKeys'
import { ExternalIndicatorsImportPage } from '@/pages/ExternalIndicatorsImport'
import { CatalogTab } from './external-indicators/CatalogTab'
import { BuiltinTab } from './external-indicators/BuiltinTab'

type TabKey = 'catalog' | 'import' | 'builtin' | 'custom'

const TABS: { key: TabKey; labelKey: string }[] = [
  { key: 'catalog', labelKey: 'page.datasets.extInd.tab.catalog' },
  { key: 'import', labelKey: 'page.datasets.extInd.tab.import' },
  { key: 'builtin', labelKey: 'page.datasets.extInd.tab.builtin' },
  { key: 'custom', labelKey: 'page.datasets.extInd.tab.custom' },
]

export function ExternalIndicatorsPage() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabKey>('catalog')
  const queryClient = useQueryClient()

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">{t('page.datasets.extInd.title')}</h1>
        <p className="page-subtitle">{t('page.datasets.extInd.subtitle')}</p>
      </div>

      {/* Tab navigation (same style as DatasetsPage) */}
      <div className="flex gap-1 border-b border-gray-200">
        {TABS.map(tab => (
          <button
            key={tab.key}
            className={`px-4 py-2 text-sm font-medium transition-colors -mb-px ${
              activeTab === tab.key
                ? 'border-b-2 border-brand-500 text-brand-600'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab(tab.key)}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>

      {activeTab === 'catalog' && <CatalogTab />}
      {activeTab === 'import' && (
        <ExternalIndicatorsImportPage
          embedded
          onImported={() => queryClient.invalidateQueries({ queryKey: queryKeys.datasets.extIndCatalog })}
        />
      )}
      {activeTab === 'builtin' && <BuiltinTab />}
      {activeTab === 'custom' && (
        <div className="card flex items-center justify-center h-64 text-gray-400 text-sm">
          {t('page.datasets.extInd.placeholder.custom')}
        </div>
      )}
    </div>
  )
}
