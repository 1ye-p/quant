import React from 'react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils';
import { downloadCsv, downloadJson } from '@/lib/download';

interface CompareMetrics {
  backtest_id: string;
  strategy_name: string;
  total_return: number;
  annualized_return: number;
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  calmar_ratio: number;
  sortino_ratio: number;
}

/** Numeric metric columns only (excludes the string identifier columns). */
type MetricKey = Exclude<keyof CompareMetrics, 'backtest_id' | 'strategy_name'>;

interface CompareMetricsTableProps {
  metrics: CompareMetrics[];
}

export const CompareMetricsTable: React.FC<CompareMetricsTableProps> = ({ metrics }) => {
  const { t } = useTranslation();
  if (metrics.length === 0) return null;

  const rows: Array<{ key: MetricKey; label: string; format: (v: number) => string; higher: boolean }> = [
    { key: 'total_return', label: t('common.metric.total_return'), format: (v) => `${(v * 100).toFixed(1)}%`, higher: true },
    { key: 'annualized_return', label: t('common.metric.annualized_return'), format: (v) => `${(v * 100).toFixed(1)}%`, higher: true },
    { key: 'sharpe_ratio', label: t('common.metric.sharpe_ratio'), format: (v) => v.toFixed(2), higher: true },
    { key: 'max_drawdown', label: t('common.metric.max_drawdown'), format: (v) => `${(v * 100).toFixed(1)}%`, higher: false },
    { key: 'win_rate', label: t('common.metric.win_rate'), format: (v) => `${(v * 100).toFixed(0)}%`, higher: true },
    { key: 'calmar_ratio', label: t('common.metric.calmar_ratio'), format: (v) => v.toFixed(2), higher: true },
    { key: 'sortino_ratio', label: t('common.metric.sortino_ratio'), format: (v) => v.toFixed(2), higher: true },
  ];

  const getBestIndex = (key: MetricKey, higher: boolean) => {
    const values = metrics.map((m) => m[key]);
    const best = higher ? Math.max(...values) : Math.min(...values);
    return values.indexOf(best);
  };

  // NOTE: this table is transposed (metrics as rows, strategies as columns),
  // which DataTable's column model cannot express, so it renders its own
  // <table> and keeps a local export toolbar instead of DataTable enableExport.
  const exportRows = metrics.map((m) => ({
    strategy_name: m.strategy_name,
    ...Object.fromEntries(rows.map((r) => [r.key, m[r.key]])),
  }));

  return (
    <div className="card overflow-hidden">
      <div className="flex justify-end gap-2 p-2 border-b">
        <button
          className="btn-secondary text-xs px-3 py-1"
          onClick={() => downloadCsv(exportRows, 'compare_metrics.csv')}
        >
          {t('component.ui.data_table.export_csv')}
        </button>
        <button
          className="btn-secondary text-xs px-3 py-1"
          onClick={() => downloadJson(metrics, 'compare_metrics.json')}
        >
          {t('component.ui.data_table.export_json')}
        </button>
      </div>
      <table className="w-full">
        <thead>
          <tr className="bg-gray-50">
            <th className="p-3 text-left font-medium">{t('component.compare.metrics_table.col.metric')}</th>
            {metrics.map(m => (
              <th key={m.backtest_id} className="p-3 text-right font-medium">{m.strategy_name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(row => {
            const bestIdx = getBestIndex(row.key, row.higher);
            return (
              <tr key={row.key} className="border-t">
                <td className="p-3 text-gray-500">{row.label}</td>
                {metrics.map((m, idx) => (
                  <td key={m.backtest_id} className={cn(
                    "p-3 text-right font-medium",
                    idx === bestIdx && "text-brand-600 font-semibold"
                  )}>
                    {row.format(m[row.key])}
                    {idx === bestIdx && <span className="ml-1">🏆</span>}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};
