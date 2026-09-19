import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { backtestsApi, type RankedStrategy } from '@/lib/api/backtests';
import { queryKeys } from '@/lib/queryKeys';
import { useBacktestCompareStore } from '@/stores/backtestCompareStore';
import { CompareMetricsTable } from '@/components/backtests/compare/CompareMetricsTable';
import { CompareNavChart } from '@/components/backtests/compare/CompareNavChart';
import { StatisticalTestPanel } from '@/components/backtests/compare/StatisticalTestPanel';
import { CompareDrawdownChart } from '@/components/backtests/compare/CompareDrawdownChart';

const RANK_DIMS = [
  'sharpe_ratio',
  'max_drawdown',
  'sortino_ratio',
  'calmar_ratio',
  'turnover',
  'oos_ratio',
  'cost_sensitivity',
] as const;

const RankTable: React.FC<{
  ranked: RankedStrategy[];
  weights: Record<string, number>;
}> = ({ ranked, weights }) => {
  const { t } = useTranslation();

  return (
    <div className="card overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-gray-50">
          <tr>
            <th className="p-3 text-left font-medium">{t('page.backtest_compare.rank.col_rank')}</th>
            <th className="p-3 text-left font-medium">{t('page.backtest_compare.rank.col_strategy')}</th>
            <th className="p-3 text-right font-medium"
                title={RANK_DIMS.map(d => `${t(`page.backtest_compare.rank.dim.${d}`)}: ${((weights[d] ?? 0) * 100).toFixed(0)}%`).join('\n')}>
              {t('page.backtest_compare.rank.col_score')} ⓘ
            </th>
            {RANK_DIMS.map(dim => (
              <th key={dim} className="p-3 text-right font-medium whitespace-nowrap"
                  title={`${t(`page.backtest_compare.rank.dim.${dim}`)} — ${t('page.backtest_compare.rank.weight')} ${((weights[dim] ?? 0) * 100).toFixed(0)}%`}>
                {t(`page.backtest_compare.rank.dim.${dim}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ranked.map(r => (
            <tr key={r.run_id} className="border-t">
              <td className="p-3 font-semibold">{r.rank === 1 ? '🏆 ' : ''}{r.rank}</td>
              <td className="p-3">{r.strategy_id}</td>
              <td className="p-3 text-right font-semibold text-brand-600">{r.composite_score.toFixed(4)}</td>
              {RANK_DIMS.map(dim => (
                <td key={dim} className="p-3 text-right">
                  {(r.dimension_scores?.[dim] ?? 0).toFixed(3)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export const BacktestComparePage: React.FC = () => {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { selectedIds, clearSelection } = useBacktestCompareStore();
  const [view, setView] = useState<'metrics' | 'rank'>('metrics');

  const { data: apiData, isLoading, error } = useQuery({
    queryKey: ['backtest-compare', selectedIds],
    queryFn: () => backtestsApi.compare(selectedIds.join(',')),
    enabled: selectedIds.length >= 2,
  });

  const { data: rankData, isLoading: rankLoading } = useQuery({
    queryKey: queryKeys.backtests.rank(selectedIds),
    queryFn: () => backtestsApi.rank({ run_ids: selectedIds }),
    enabled: selectedIds.length >= 2 && view === 'rank',
  });

  // Transform API response to component props
  const metrics = apiData?.runs?.map(run => ({
    backtest_id: run.run_id,
    strategy_name: run.strategy_id,
    total_return: run.metrics?.total_return ?? 0,
    annualized_return: run.metrics?.annualized_return ?? 0,
    sharpe_ratio: run.metrics?.sharpe_ratio ?? 0,
    max_drawdown: run.metrics?.max_drawdown ?? 0,
    win_rate: run.metrics?.win_rate ?? 0,
    calmar_ratio: run.metrics?.calmar_ratio ?? 0,
    sortino_ratio: run.metrics?.sortino_ratio ?? 0,
  }));

  const navCurves = apiData?.runs?.map(run => ({
    backtest_id: run.run_id,
    strategy_name: run.strategy_id,
    data: run.nav_series?.map(p => ({ date: p.date, value: p.nav })) ?? [],
  }));

  const backtestNames = Object.fromEntries(
    (apiData?.runs ?? []).map(r => [r.run_id, r.strategy_id]),
  );

  // Compute drawdown series from NAV data
  const drawdowns = apiData?.runs?.map(run => {
    const navs = run.nav_series ?? [];
    let peak = -Infinity;
    const data = navs.map(p => {
      if (p.nav > peak) peak = p.nav;
      return {
        date: p.date,
        drawdown: peak > 0 ? (p.nav - peak) / peak : 0,
      };
    });
    return { backtest_id: run.run_id, name: run.strategy_id, data };
  });

  if (selectedIds.length < 2) {
    return (
      <div className="space-y-4">
        <div className="flex justify-between items-center">
          <h1 className="page-title">{t('page.backtest_compare.title')}</h1>
          <button onClick={() => navigate('/backtests')} className="btn-secondary">{t('page.backtest_compare.action.back')}</button>
        </div>
        <div className="card p-8 text-center text-gray-400">
          {t('page.backtest_compare.empty.select_hint')}
        </div>
      </div>
    );
  }

  if (isLoading) return <div>{t('common.loading')}</div>;
  if (error) return <div className="card p-8 text-center text-red-500">{t('page.backtest_compare.error.load_failed', { message: (error as Error).message })}</div>;

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="page-title">{t('page.backtest_compare.title')}</h1>
        <div className="flex gap-2">
          <button onClick={clearSelection} className="btn-secondary">{t('page.backtest_compare.action.clear')}</button>
          <button onClick={() => navigate('/backtests')} className="btn-secondary">{t('page.backtest_compare.action.back')}</button>
        </div>
      </div>

      {/* View toggle: raw metrics ↔ rank */}
      <div className="flex gap-2">
        <button
          className={view === 'metrics' ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
          onClick={() => setView('metrics')}
        >
          {t('page.backtest_compare.rank.view_metrics')}
        </button>
        <button
          className={view === 'rank' ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
          onClick={() => setView('rank')}
        >
          {t('page.backtest_compare.rank.view_rank')}
        </button>
      </div>

      {view === 'metrics' && metrics && metrics.length > 0 && (
        <CompareMetricsTable metrics={metrics} />
      )}
      {view === 'rank' && (
        rankLoading ? <div>{t('common.loading')}</div> :
        rankData?.ranked_strategies?.length ? (
          <div className="space-y-2">
            <div className="text-xs text-gray-400">{t('page.backtest_compare.rank.weights_hint')}</div>
            <RankTable ranked={rankData.ranked_strategies} weights={rankData.weights ?? {}} />
          </div>
        ) : (
          <div className="card p-8 text-center text-gray-400">{t('page.backtest_compare.rank.empty')}</div>
        )
      )}
      {navCurves && navCurves.length > 0 && <CompareNavChart curves={navCurves} />}
      {drawdowns && drawdowns.length > 0 && <CompareDrawdownChart drawdowns={drawdowns} />}
      <StatisticalTestPanel backtestIds={selectedIds} backtestNames={backtestNames} />
    </div>
  );
};
