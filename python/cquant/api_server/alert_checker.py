"""alert_checker — 告警规则检查引擎。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RULE_TYPES = {
    "data_stale":      "数据缺失超过 N 天",
    "factor_ic_low":   "因子 IC 绝对值持续低于阈值",
    "pnl_drawdown":    "策略 P&L 回撤超过阈值",
    "risk_breach":     "风控策略拦截交易",
    "news_sentiment":  "组合新闻情感恶化",
}


_alert_tables_ensured = False


def _ensure_tables(catalog) -> None:
    global _alert_tables_ensured
    if _alert_tables_ensured:
        return
    try:
        catalog.execute("""
            CREATE TABLE IF NOT EXISTS meta_alert_rules (
                rule_id      VARCHAR PRIMARY KEY,
                rule_type    VARCHAR NOT NULL,
                params_json  VARCHAR NOT NULL,
                enabled      BOOLEAN DEFAULT TRUE,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        catalog.execute("""
            CREATE TABLE IF NOT EXISTS meta_alert_history (
                alert_id     VARCHAR PRIMARY KEY,
                rule_id      VARCHAR NOT NULL,
                rule_type    VARCHAR NOT NULL,
                severity     VARCHAR DEFAULT 'warning',
                message      VARCHAR NOT NULL,
                triggered_at TIMESTAMP NOT NULL,
                read         BOOLEAN DEFAULT FALSE
            )
        """)
        # Migration: add severity column to existing tables
        catalog.execute(
            "ALTER TABLE meta_alert_history ADD COLUMN IF NOT EXISTS severity VARCHAR DEFAULT 'warning'"
        )
        _alert_tables_ensured = True
    except Exception as exc:
        logger.debug("_ensure_tables: %s", exc)


def _save_alert(catalog, rule_id: str, rule_type: str, message: str, severity: str = "warning") -> None:
    alert_id = f"al_{uuid.uuid4().hex[:10]}"
    catalog.execute(
        "INSERT INTO meta_alert_history (alert_id, rule_id, rule_type, severity, message, triggered_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [alert_id, rule_id, rule_type, severity, message,
         datetime.now(tz=timezone.utc).isoformat()],
    )


def check_data_stale(catalog, rule_id: str, params: dict) -> bool:
    """检查数据是否超过 N 天未更新。"""
    max_days = int(params.get("max_days", 2))
    try:
        df = catalog.query(
            "SELECT MAX(trade_date) as d FROM silver_prices_1d"
        )
        if df.is_empty() or df["d"][0] is None:
            _save_alert(catalog, rule_id, "data_stale", "无可用行情数据", severity="warning")
            return True
        from datetime import date
        days = (date.today() - df["d"][0]).days
        if days > max_days:
            _save_alert(catalog, rule_id, "data_stale",
                        f"行情数据已 {days} 天未更新（最新：{df['d'][0]}，阈值：{max_days}天）",
                        severity="warning")
            return True
    except Exception as e:
        logger.warning("check_data_stale failed: %s", e)
    return False


def check_factor_ic_low(catalog, rule_id: str, params: dict) -> bool:
    """检查因子 IC 是否持续低于阈值。"""
    factor_name = params.get("factor_name", "")
    threshold = float(params.get("threshold", 0.02))
    window_days = int(params.get("window_days", 20))
    if not factor_name:
        return False
    try:
        df = catalog.query(
            "SELECT ic_mean FROM gold_factor_ic_summary "
            "WHERE factor_name = ? "
            "  AND updated_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 DAY') "
            "ORDER BY updated_at DESC LIMIT 1",
            [factor_name, window_days],
        )
        if df.is_empty():
            return False
        ic = float(df["ic_mean"][0])
        if abs(ic) < threshold:
            _save_alert(catalog, rule_id, "factor_ic_low",
                        f"因子 {factor_name} IC 绝对值 {abs(ic):.4f} 低于阈值 {threshold}（近{window_days}日）",
                        severity="warning")
            return True
    except Exception as e:
        logger.warning("check_factor_ic_low failed: %s", e)
    return False


def check_pnl_drawdown(catalog, rule_id: str, params: dict) -> bool:
    """检查策略最大回撤是否超过阈值。"""
    strategy_id = params.get("strategy_id", "")
    threshold_pct = float(params.get("threshold_pct", 10.0)) / 100
    if not strategy_id:
        return False
    try:
        # Anchor to the single most recently completed run to avoid mixing history
        df = catalog.query(
            "SELECT MAX(ABS(rs.drawdown)) as max_dd "
            "FROM gold_risk_snapshots rs "
            "WHERE rs.run_id = ("
            "  SELECT run_id FROM gold_backtest_runs "
            "  WHERE strategy_id = ? AND status = 'completed' "
            "  ORDER BY completed_at DESC LIMIT 1"
            ")",
            [strategy_id],
        )
        if df.is_empty() or df["max_dd"][0] is None:
            return False
        max_dd = float(df["max_dd"][0])  # ABS already applied in SQL
        if max_dd > threshold_pct:
            severity = "critical" if max_dd > 0.10 else "warning"
            _save_alert(catalog, rule_id, "pnl_drawdown",
                        f"策略 {strategy_id} 最大回撤 {max_dd*100:.2f}% 超过阈值 {threshold_pct*100:.2f}%",
                        severity=severity)
            return True
    except Exception as e:
        logger.warning("check_pnl_drawdown failed: %s", e)
    return False


def check_news_sentiment(catalog, rule_id: str, params: dict) -> bool:
    """检查组合持仓的新闻情感是否恶化。

    Parameters in *params*:
    - threshold:          sentiment score 低于此值时触发（默认 -0.5）
    - change_threshold:   日间情感变化量低于此值时触发（默认 -0.3）
    - scope:              "portfolio" | "all" — 检查范围（默认 "portfolio"）
    - critical_events:    触发 critical 级别告警的事件类型列表
    """
    threshold = float(params.get("threshold", -0.5))
    change_threshold = float(params.get("change_threshold", -0.3))
    scope = params.get("scope", "portfolio")
    _VALID_SCOPES = {"portfolio", "all"}
    if scope not in _VALID_SCOPES:
        logger.warning("check_news_sentiment: invalid scope '%s', defaulting to 'portfolio'", scope)
        scope = "portfolio"
    critical_events = set(params.get("critical_events", [
        "earnings_warning", "regulatory_action", "suspension",
    ]))

    # Collect asset IDs to check
    asset_ids: set[str] = set()
    if scope == "portfolio":
        asset_ids = _get_portfolio_asset_ids(catalog)
    if not asset_ids and scope == "portfolio":
        logger.debug("check_news_sentiment: no portfolio positions found, skipping")
        return False

    triggered = False

    for aid in asset_ids:
        # Latest day sentiment
        latest_df = catalog.query(
            "SELECT DATE_TRUNC('day', published_at) as d, "
            "  AVG(sentiment_score) as avg_s, COUNT(*) as n "
            "FROM silver_news_events "
            "WHERE list_contains(asset_ids_mentioned, ?) "
            "  AND published_at >= CURRENT_DATE - INTERVAL '3' DAY "
            "  AND sentiment_score IS NOT NULL "
            "GROUP BY 1 ORDER BY 1 DESC LIMIT 2",
            [aid],
        )
        if latest_df.is_empty():
            continue
        rows = latest_df.to_dicts()
        latest_score = float(rows[0]["avg_s"])

        # Check absolute threshold
        if latest_score < threshold:
            _save_alert(
                catalog, rule_id, "news_sentiment",
                f"[{aid}] 新闻情感 {latest_score:.3f} 低于阈值 {threshold}",
                severity="warning",
            )
            triggered = True

        # Check day-over-day change
        if len(rows) >= 2:
            prev_score = float(rows[1]["avg_s"])
            change = latest_score - prev_score
            if change < change_threshold:
                _save_alert(
                    catalog, rule_id, "news_sentiment",
                    f"[{aid}] 新闻情感日变化 {change:+.3f} 低于阈值 {change_threshold} "
                    f"({prev_score:.3f} -> {latest_score:.3f})",
                    severity="warning",
                )
                triggered = True

        # Check critical event types
        if critical_events:
            ph = ", ".join("?" * len(critical_events))
            crit_df = catalog.query(
                f"SELECT event_id, headline, event_type, published_at "
                f"FROM silver_news_events "
                f"WHERE list_contains(asset_ids_mentioned, ?) "
                f"  AND event_type IN ({ph}) "
                f"  AND published_at >= CURRENT_DATE - INTERVAL '1' DAY "
                f"ORDER BY published_at DESC LIMIT 5",
                [aid, *critical_events],
            )
            for row in crit_df.to_dicts():
                _save_alert(
                    catalog, rule_id, "news_sentiment",
                    f"[{aid}] 关键事件 ({row['event_type']}): {row['headline'][:80]}",
                    severity="critical",
                )
                triggered = True

    # If scope is "all", also check recent critical events across all assets
    if scope == "all" and critical_events:
        ph = ", ".join("?" * len(critical_events))
        crit_df = catalog.query(
            f"SELECT event_id, headline, event_type, asset_ids_mentioned, published_at "
            f"FROM silver_news_events "
            f"WHERE event_type IN ({ph}) "
            f"  AND published_at >= CURRENT_DATE - INTERVAL '1' DAY "
            f"ORDER BY published_at DESC LIMIT 10",
            list(critical_events),
        )
        for row in crit_df.to_dicts():
            assets = row.get("asset_ids_mentioned") or []
            aid_label = ", ".join(assets[:3]) if assets else "market"
            _save_alert(
                catalog, rule_id, "news_sentiment",
                f"[{aid_label}] 关键事件 ({row['event_type']}): {row['headline'][:80]}",
                severity="critical",
            )
            triggered = True

    return triggered


def _get_portfolio_asset_ids(catalog) -> set[str]:
    """从 PaperBroker 持仓和 gold_live_executions 中收集当前持仓资产 ID。"""
    asset_ids: set[str] = set()

    # Method 1: Try the global PaperBroker singleton
    try:
        from cquant.api_server.routes.trading import _get_paper_broker
        broker = _get_paper_broker()
        positions = broker.get_positions()
        asset_ids.update(positions.keys())
    except Exception as exc:
        logger.debug("_get_portfolio_asset_ids (paper_broker): %s", exc)

    # Method 2: From persisted executions — compute net positions
    try:
        df = catalog.query(
            "SELECT asset_id, side, filled_qty "
            "FROM gold_live_executions WHERE status = 'filled' "
            "ORDER BY executed_at"
        )
        if not df.is_empty():
            net: dict[str, int] = {}
            for row in df.to_dicts():
                aid = row["asset_id"]
                qty = int(row["filled_qty"])
                if row["side"] == "buy":
                    net[aid] = net.get(aid, 0) + qty
                else:
                    net[aid] = net.get(aid, 0) - qty
            asset_ids.update(aid for aid, q in net.items() if q > 0)
    except Exception as exc:
        logger.debug("_get_portfolio_asset_ids (executions): %s", exc)

    return asset_ids


CHECKERS = {
    "data_stale":    check_data_stale,
    "factor_ic_low": check_factor_ic_low,
    "pnl_drawdown":  check_pnl_drawdown,
    "news_sentiment": check_news_sentiment,
}


def run_all_checks(catalog) -> int:
    """运行所有启用的告警规则，返回触发数量。"""
    _ensure_tables(catalog)
    rules_df = catalog.query(
        "SELECT rule_id, rule_type, params_json FROM meta_alert_rules WHERE enabled = TRUE"
    )
    if rules_df.is_empty():
        return 0

    triggered = 0
    for row in rules_df.to_dicts():
        try:
            params = json.loads(row["params_json"])
            checker = CHECKERS.get(row["rule_type"])
            if checker and checker(catalog, row["rule_id"], params):
                triggered += 1
        except Exception as e:
            logger.warning("Alert check failed for %s: %s", row["rule_id"], e)
    return triggered
