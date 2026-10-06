"""cquant.backtest_vector.fill_simulator — A-share fill simulation engine.

Simulates realistic order execution with A-share market constraints:
- Price limit (涨跌停): orders at limit price cannot execute
- Suspension (停牌): suspended stocks cannot trade
- T+1 settlement: shares bought today cannot be sold until tomorrow
- Lot size (手数): orders must be multiples of 100 shares
- Tick size: price must be multiples of 0.01

Produces real fills with commission, stamp duty, and slippage costs.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import polars as pl

from cquant.backtest_vector.costs import CostModel
from cquant.backtest_vector.limit_rules import is_at_limit_up as _is_limit_up, is_at_limit_down as _is_limit_down
from cquant.core.enums import TradabilityReason
from cquant.market_calendar.rules.base import TradabilityResult

logger = logging.getLogger(__name__)

# A-share constants
_LOT_SIZE = 100
_TICK_SIZE = Decimal("0.01")


@dataclass
class FillRecord:
    """A single order fill."""
    trade_date: date
    asset_id: str
    side: str  # "buy" or "sell"
    qty: int
    price: float
    notional: float
    commission: float
    stamp_duty: float
    slippage: float
    total_cost: float


class AShareFillSimulator:
    """Simulates A-share order execution with market constraints.

    Usage::

        sim = AShareFillSimulator(cost_model=CostModel.for_cn())
        fills = sim.simulate(
            target_weights=weights_df,
            prices=prices_df,
            initial_cash=Decimal("1000000"),
        )
    """

    def __init__(
        self,
        cost_model: CostModel | None = None,
        market: str = "CN",
        adj_type: str = "forward",
        catalog=None,
        max_volume_pct: float = 0.1,
    ) -> None:
        self._cost_model = cost_model or CostModel.for_cn()
        self._market = market
        self._adj_type = adj_type  # DEPRECATED: stored but unused; raw_close now captures unadjusted price
        self._catalog = catalog
        self._max_volume_pct = max(0.01, min(1.0, max_volume_pct))
        self._rules = None
        self._status_tracker = None
        # Lazy-init market rules if market_calendar is available
        try:
            from cquant.market_calendar import get_market_rules, load_market_config, StatusTracker
            config = load_market_config(market)
            self._rules = get_market_rules(market, config)
            self._status_tracker = StatusTracker()
            self._rules.set_status_tracker(self._status_tracker)
        except Exception:
            logger.debug("Market rules not available, using legacy limit_rules")

    def _emit_risk_alert(
        self, policy_name: str, decision: str, reason: str, asset_id: str, trade_date: date
    ) -> None:
        """Emit a risk breach alert to the alert history table."""
        if not self._catalog:
            return
        severity = "critical" if decision == "REJECTED" else "warning"
        alert_id = f"al_{uuid.uuid4().hex[:10]}"
        message = f"[{decision}] {policy_name}: {reason} (资产: {asset_id}, 日期: {trade_date})"
        try:
            self._catalog.execute(
                "INSERT OR IGNORE INTO meta_alert_history "
                "(alert_id, rule_id, rule_type, severity, message, triggered_at, read) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [alert_id, f"risk_{policy_name}", "risk_breach", severity, message,
                 datetime.now(tz=timezone.utc).isoformat(), False],
            )
        except Exception:
            pass

    def simulate(
        self,
        target_weights: pl.DataFrame,
        prices: pl.DataFrame,
        initial_cash: Decimal,
        suspension_col: str = "is_suspended",
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Simulate fills from target weights and prices.

        Args:
            target_weights: [trade_date, asset_id, target_weight]
            prices: [asset_id, trade_date, open, high, low, close, volume, is_suspended]
            initial_cash: Starting cash amount
            suspension_col: Column name for suspension flag

        Returns:
            (fills_df, portfolio_snapshots_df)
        """
        if target_weights.is_empty():
            return self._empty_fills(), self._empty_snapshots()

        # Build price lookup: (trade_date, asset_id) -> price data
        price_lookup = self._build_price_lookup(prices, suspension_col)

        # Track portfolio state
        cash = float(initial_cash)
        positions: dict[str, int] = {}  # asset_id -> qty
        buy_dates: dict[str, date] = {}  # asset_id -> date of most recent buy (for T+1)

        fills: list[dict] = []
        snapshots: list[dict] = []

        # Get all trade dates in order
        trade_dates = sorted(target_weights["trade_date"].unique().to_list())

        for td in trade_dates:
            # Get target weights for this date
            day_weights = target_weights.filter(pl.col("trade_date") == td)
            target_dict = dict(zip(
                day_weights["asset_id"].to_list(),
                day_weights["target_weight"].to_list(),
            ))

            # Calculate current NAV for weight-based sizing.
            # NOTE (quirk, preserved): this is the *pre-sell* NAV — sell/buy
            # sizing below uses the start-of-day portfolio value (recorded
            # 2026-08-09 v2.0); do not "fix".
            nav = self._calculate_nav(cash, positions, td, price_lookup)
            n_fills_before_day = len(fills)

            # Process sells first (to free up cash)
            for asset_id in list(positions.keys()):
                if positions[asset_id] <= 0:
                    continue

                target_w = target_dict.get(asset_id, 0.0)
                current_w = (positions[asset_id] * self._get_price(td, asset_id, price_lookup, "close")) / nav if nav > 0 else 0

                # Need to sell if current weight > target weight
                if current_w > target_w + 0.001:  # 0.1% tolerance
                    sell_qty = self._calculate_sell_qty(
                        positions[asset_id], current_w, target_w, nav,
                        td, asset_id, price_lookup, buy_dates
                    )
                    if sell_qty > 0:
                        fill = self._execute_sell(td, asset_id, sell_qty, price_lookup)
                        if fill:
                            fills.append(fill)
                            cash += fill["notional"] - fill["total_cost"]
                            positions[asset_id] -= fill["qty"]
                            if positions[asset_id] <= 0:
                                del positions[asset_id]

            # Process buys
            for asset_id, target_w in target_dict.items():
                if target_w <= 0:
                    continue

                current_qty = positions.get(asset_id, 0)
                current_w = (current_qty * self._get_price(td, asset_id, price_lookup, "close")) / nav if nav > 0 else 0

                # Need to buy if current weight < target weight
                if current_w < target_w - 0.001:
                    buy_qty = self._calculate_buy_qty(
                        cash, nav, target_w, current_w,
                        td, asset_id, price_lookup
                    )
                    if buy_qty > 0:
                        fill = self._execute_buy(td, asset_id, buy_qty, price_lookup)
                        if fill:
                            fills.append(fill)
                            cash -= fill["notional"] + fill["total_cost"]
                            positions[asset_id] = current_qty + fill["qty"]
                            buy_dates[asset_id] = td

            # Daily snapshot.
            # NAV dedupe: cash/positions are only mutated inside `if fill:`
            # branches above, so on a no-fill day the state at this point is
            # identical to the pre-sell NAV call — _calculate_nav is a pure
            # function of (cash, positions, td, lookup), so reuse is
            # bitwise-identical to recomputing. On any-fill days, recompute.
            if len(fills) > n_fills_before_day:
                nav = self._calculate_nav(cash, positions, td, price_lookup)
            snapshots.append({
                "trade_date": td,
                "cash": cash,
                "nav": nav,
                "positions_count": len(positions),
                "gross_exposure": sum(
                    qty * self._get_price(td, aid, price_lookup, "close")
                    for aid, qty in positions.items()
                ),
            })

        fills_df = pl.DataFrame(fills) if fills else self._empty_fills()
        snapshots_df = pl.DataFrame(snapshots) if snapshots else self._empty_snapshots()

        return fills_df, snapshots_df

    def _build_price_lookup(
        self, prices: pl.DataFrame, suspension_col: str
    ) -> dict[tuple[date, str], dict]:
        """Build lookup dict: (trade_date, asset_id) -> {open, high, low, close, volume, is_suspended, prev_close}"""
        # First pass: collect all data
        raw: dict[tuple[date, str], dict] = {}
        for row in prices.iter_rows(named=True):
            td = row["trade_date"]
            aid = row["asset_id"]
            raw[(td, aid)] = {
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "close": float(row.get("close", 0) or 0),
                "volume": float(row.get("volume", 0) or 0),
                "is_suspended": bool(row.get(suspension_col, False)),
                "adj_factor": float(row.get("adj_factor", 1.0) or 1.0),
            }

        # Second pass: compute prev_close from actual previous day's close.
        # Per asset: sort its dates ONCE, then a single forward walk carrying
        # the previous close — O(D log D) per asset instead of the former
        # per-row sorted()+.index() O(D^2) rescan (93% of simulate cumtime at
        # 5000x500, see artifacts/benchmarks/2026-10-05_pre_perf_fill.json).
        # Semantics unchanged: prev_close = close of the previous *available*
        # date for that asset; first day uses its own close.
        asset_dates: dict[str, list[date]] = {}
        for (td, aid) in raw:
            asset_dates.setdefault(aid, []).append(td)

        lookup: dict[tuple[date, str], dict] = {}
        for aid, dates in asset_dates.items():
            dates.sort()
            prev_close: float | None = None
            for td in dates:
                data = raw[(td, aid)]
                lookup[(td, aid)] = {
                    **data,
                    "prev_close": data["close"] if prev_close is None else prev_close,
                }
                prev_close = data["close"]
        return lookup

    def _get_price(self, td: date, asset_id: str, lookup: dict, field: str = "close") -> float:
        """Get price from lookup, return 0 if not found."""
        data = lookup.get((td, asset_id), {})
        return data.get(field, 0.0)

    def _is_suspended(self, td: date, asset_id: str, lookup: dict) -> bool:
        """Check if stock is suspended."""
        data = lookup.get((td, asset_id), {})
        return data.get("is_suspended", False)

    def _is_at_limit_up(self, td: date, asset_id: str, lookup: dict) -> bool:
        """Check if stock is at price limit up (涨停)."""
        key = (td, asset_id)
        if key not in lookup:
            return False
        row = lookup[key]
        close, high, prev_close = row["close"], row["high"], row.get("prev_close", 0)
        if prev_close <= 0:
            return False
        return _is_limit_up(close, prev_close, asset_id) and close == high

    def _is_at_limit_down(self, td: date, asset_id: str, lookup: dict) -> bool:
        """Check if stock is at price limit down (跌停)."""
        key = (td, asset_id)
        if key not in lookup:
            return False
        row = lookup[key]
        close, low, prev_close = row["close"], row["low"], row.get("prev_close", 0)
        if prev_close <= 0:
            return False
        return _is_limit_down(close, prev_close, asset_id) and close == low

    def _can_sell(self, td: date, asset_id: str, buy_dates: dict) -> bool:
        """Check T+1 constraint: can only sell if bought before today."""
        last_buy = buy_dates.get(asset_id)
        if last_buy is None:
            return True  # Position existed before backtest start
        return last_buy < td  # Must be strictly before today

    def _is_price_valid(self, td: date, asset_id: str, lookup: dict) -> bool:
        """Check if close price is within reasonable range of prev_close.

        Rejects prices that exceed ±30% from prev_close, which catches data quality
        issues (e.g., close=0.01 with high=3.00) while allowing legitimate limit moves.
        The more precise limit_up/limit_down checks handle actual trading constraints.
        """
        data = lookup.get((td, asset_id), {})
        if not data:
            return False
        close = data.get("close", 0)
        prev_close = data.get("prev_close", 0)
        if prev_close <= 0 or close <= 0:
            return False
        ratio = close / prev_close
        return 0.5 <= ratio <= 2.0

    def _check_tradability(self, td: date, asset_id: str, lookup: dict) -> TradabilityResult:
        """Check tradability using market rules layer if available, else legacy."""
        if self._rules:
            data = lookup.get((td, asset_id), {})
            bar = {
                "open": data.get("open", 0),
                "high": data.get("high", 0),
                "low": data.get("low", 0),
                "close": data.get("close", 0),
                "volume": data.get("volume", 0),
                "pre_close": data.get("prev_close", data.get("close", 0)),
            }
            return self._rules.check_tradable(asset_id, td, bar)

        # Legacy fallback
        if self._is_suspended(td, asset_id, lookup):
            return TradabilityResult(False, TradabilityReason.SUSPENDED)
        return TradabilityResult(True, TradabilityReason.TRADABLE)

    def _round_lot(self, qty: float) -> int:
        """Round down to nearest lot (100 shares)."""
        return int(qty // _LOT_SIZE) * _LOT_SIZE

    def _apply_volume_constraint(
        self, qty: int, td: date, asset_id: str, lookup: dict
    ) -> tuple[int, float]:
        """Apply volume participation constraint to order quantity.

        If order qty exceeds volume * max_volume_pct, clip the order and
        compute additional slippage based on participation rate.

        Returns:
            Tuple of (clipped_qty, volume_slippage_pct)
        """
        data = lookup.get((td, asset_id), {})
        volume = data.get("volume", 0)

        if volume <= 0 or qty <= 0:
            return qty, 0.0

        max_qty = int(volume * self._max_volume_pct)
        max_qty = self._round_lot(max_qty)

        if max_qty <= 0:
            return 0, 0.0

        clipped_qty = min(qty, max_qty)

        # Volume-based slippage: square-root model
        # Higher participation rate = higher slippage
        participation_rate = clipped_qty / volume
        volume_slippage_pct = 0.0
        if participation_rate > 0.01:  # Only apply if > 1% participation
            volume_slippage_pct = 0.001 * (participation_rate / self._max_volume_pct) ** 0.5
            volume_slippage_pct = min(volume_slippage_pct, 0.01)  # Cap at 1%

        return clipped_qty, volume_slippage_pct

    def _calculate_nav(
        self, cash: float, positions: dict[str, int], td: date, lookup: dict
    ) -> float:
        """Calculate total portfolio NAV."""
        market_value = sum(
            qty * self._get_price(td, aid, lookup, "close")
            for aid, qty in positions.items()
        )
        return cash + market_value

    def _calculate_sell_qty(
        self,
        current_qty: int,
        current_w: float,
        target_w: float,
        nav: float,
        td: date,
        asset_id: str,
        lookup: dict,
        buy_dates: dict,
    ) -> int:
        """Calculate quantity to sell."""
        # Check T+1
        if not self._can_sell(td, asset_id, buy_dates):
            self._emit_risk_alert("T+1", "REJECTED", "T+1 限制，当日买入不可卖出", asset_id, td)
            return 0

        # Use market rules layer if available
        if self._rules:
            result = self._check_tradability(td, asset_id, lookup)
            if not result.tradable:
                # Handle delist forced liquidation
                if result.reason == TradabilityReason.DELISTED:
                    positions = {asset_id: current_qty}
                    forced = self._rules.handle_delist(positions, asset_id, td,
                                                       self._get_price(td, asset_id, lookup, "close"))
                    if forced:
                        return forced[0].qty
                self._emit_risk_alert("tradability", "REJECTED",
                                      f"不可交易: {result.reason}", asset_id, td)
                return 0
        else:
            # Legacy checks
            if self._is_suspended(td, asset_id, lookup):
                self._emit_risk_alert("suspension", "REJECTED", "停牌", asset_id, td)
                return 0
            if self._is_at_limit_down(td, asset_id, lookup):
                self._emit_risk_alert("limit_down", "REJECTED", "跌停无法卖出", asset_id, td)
                return 0

        # Check price validity (reject bad data)
        if not self._is_price_valid(td, asset_id, lookup):
            self._emit_risk_alert("price_validity", "REJECTED", "价格数据异常", asset_id, td)
            return 0

        price = self._get_price(td, asset_id, lookup, "close")
        if price <= 0:
            return 0

        # Calculate target sell quantity
        target_value = target_w * nav
        current_value = current_qty * price
        sell_value = current_value - target_value
        sell_qty = self._round_lot(sell_value / price)

        return min(sell_qty, current_qty)

    def _calculate_buy_qty(
        self,
        cash: float,
        nav: float,
        target_w: float,
        current_w: float,
        td: date,
        asset_id: str,
        lookup: dict,
    ) -> int:
        """Calculate quantity to buy."""
        # Use market rules layer if available
        if self._rules:
            result = self._check_tradability(td, asset_id, lookup)
            if not result.tradable:
                self._emit_risk_alert("tradability", "REJECTED",
                                      f"不可交易: {result.reason}", asset_id, td)
                return 0
        else:
            # Legacy checks
            if self._is_suspended(td, asset_id, lookup):
                self._emit_risk_alert("suspension", "REJECTED", "停牌", asset_id, td)
                return 0
            if self._is_at_limit_up(td, asset_id, lookup):
                self._emit_risk_alert("limit_up", "REJECTED", "涨停无法买入", asset_id, td)
                return 0

        # Check price validity (reject bad data)
        if not self._is_price_valid(td, asset_id, lookup):
            self._emit_risk_alert("price_validity", "REJECTED", "价格数据异常", asset_id, td)
            return 0

        price = self._get_price(td, asset_id, lookup, "close")
        if price <= 0:
            return 0

        # Calculate target buy quantity
        target_value = target_w * nav
        current_value = current_w * nav
        buy_value = min(target_value - current_value, cash * 0.98)  # Leave 2% buffer
        buy_qty = self._round_lot(buy_value / price)

        return max(buy_qty, 0)

    def _execute_sell(
        self, td: date, asset_id: str, qty: int, lookup: dict
    ) -> dict | None:
        """Execute a sell order and return fill record."""
        price = self._get_price(td, asset_id, lookup, "close")
        if price <= 0 or qty <= 0:
            return None

        # Apply volume participation constraint
        clipped_qty, volume_slippage_pct = self._apply_volume_constraint(
            qty, td, asset_id, lookup
        )
        if clipped_qty <= 0:
            return None

        notional = clipped_qty * price
        commission = float(self._cost_model.commission(Decimal(str(notional))))
        stamp_duty = float(self._cost_model.stamp_duty(Decimal(str(notional)), is_sell=True))
        base_slippage = float(self._cost_model.slippage(Decimal(str(notional))))
        # Add volume-based slippage
        volume_slippage = notional * volume_slippage_pct
        slippage = base_slippage + volume_slippage
        total_cost = commission + stamp_duty + slippage

        # raw_close: unadjusted execution price (price is the adjusted close,
        # so raw_close = price / adj_factor recovers the true traded price).
        adj_factor = self._get_price(td, asset_id, lookup, "adj_factor") or 1.0
        raw_close = price / adj_factor if adj_factor > 0 else price

        return {
            "trade_date": td,
            "asset_id": asset_id,
            "side": "sell",
            "qty": clipped_qty,
            "price": price,
            "notional": notional,
            "commission": commission,
            "stamp_duty": stamp_duty,
            "slippage": slippage,
            "total_cost": total_cost,
            "raw_close": raw_close,
        }

    def _execute_buy(
        self, td: date, asset_id: str, qty: int, lookup: dict
    ) -> dict | None:
        """Execute a buy order and return fill record."""
        price = self._get_price(td, asset_id, lookup, "close")
        if price <= 0 or qty <= 0:
            return None

        # Apply volume participation constraint
        clipped_qty, volume_slippage_pct = self._apply_volume_constraint(
            qty, td, asset_id, lookup
        )
        if clipped_qty <= 0:
            return None

        notional = clipped_qty * price
        commission = float(self._cost_model.commission(Decimal(str(notional))))
        stamp_duty = float(self._cost_model.stamp_duty(Decimal(str(notional)), is_sell=False))
        base_slippage = float(self._cost_model.slippage(Decimal(str(notional))))
        # Add volume-based slippage
        volume_slippage = notional * volume_slippage_pct
        slippage = base_slippage + volume_slippage
        total_cost = commission + stamp_duty + slippage

        # raw_close: unadjusted execution price (price is the adjusted close,
        # so raw_close = price / adj_factor recovers the true traded price).
        adj_factor = self._get_price(td, asset_id, lookup, "adj_factor") or 1.0
        raw_close = price / adj_factor if adj_factor > 0 else price

        return {
            "trade_date": td,
            "asset_id": asset_id,
            "side": "buy",
            "qty": clipped_qty,
            "price": price,
            "notional": notional,
            "commission": commission,
            "stamp_duty": stamp_duty,
            "slippage": slippage,
            "total_cost": total_cost,
            "raw_close": raw_close,
        }

    def _empty_fills(self) -> pl.DataFrame:
        """Return empty fills DataFrame with correct schema."""
        return pl.DataFrame(schema={
            "trade_date": pl.Date,
            "asset_id": pl.Utf8,
            "side": pl.Utf8,
            "qty": pl.Int64,
            "price": pl.Float64,
            "notional": pl.Float64,
            "commission": pl.Float64,
            "stamp_duty": pl.Float64,
            "slippage": pl.Float64,
            "total_cost": pl.Float64,
            "raw_close": pl.Float64,
        })

    def _empty_snapshots(self) -> pl.DataFrame:
        """Return empty snapshots DataFrame with correct schema."""
        return pl.DataFrame(schema={
            "trade_date": pl.Date,
            "cash": pl.Float64,
            "nav": pl.Float64,
            "positions_count": pl.Int64,
            "gross_exposure": pl.Float64,
        })
