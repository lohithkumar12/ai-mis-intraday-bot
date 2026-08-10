"""
backtest.py — Historical backtest for strategy comparison
================================================================
Examples:
  python backtest.py --market INDIA --compare --timeframe 1Day
  python backtest.py --market INDIA --compare --symbols RELIANCE,TCS,INFY
  python backtest.py --demo --compare
  python backtest.py --csv path/to/bars.csv --strategy regime_adaptive
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

import config
from strategy import create_strategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("backtest")


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    equity_curve: list[float] = field(default_factory=list)


COMPARE_STRATEGIES = ("trend_pullback", "mean_reversion", "regime_adaptive", "breakout")


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    rename = {}
    for need in ("open", "high", "low", "close", "volume"):
        if need in cols:
            rename[cols[need]] = need
    if "timestamp" in cols:
        rename[cols["timestamp"]] = "timestamp"
    elif "date" in cols:
        rename[cols["date"]] = "timestamp"
    df = df.rename(columns=rename)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df.sort_index()


def synthetic_bars(n: int = 400, seed: int = 42) -> pd.DataFrame:
    """Deterministic demo series for offline smoke tests."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0004, 0.012, n)
    close = 100 * np.cumprod(1 + rets)
    high = close * (1 + rng.uniform(0.001, 0.01, n))
    low = close * (1 - rng.uniform(0.001, 0.01, n))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    volume = rng.integers(100_000, 2_000_000, n).astype(float)
    idx = pd.date_range("2023-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def run_backtest(
    df: pd.DataFrame,
    strategy_name: str,
    market: str = "US",
    symbol: str = "SYM",
    initial_equity: float = 100_000.0,
    risk_per_trade: float | None = None,
    atr_stop_mult: float | None = None,
    take_profit_r: float | None = None,
) -> BacktestResult:
    risk_pct = risk_per_trade if risk_per_trade is not None else config.RISK_PER_TRADE
    atr_mult = atr_stop_mult if atr_stop_mult is not None else config.ATR_STOP_MULT
    tp_r = take_profit_r if take_profit_r is not None else config.TAKE_PROFIT_R
    slip = float(getattr(config, "BT_SLIPPAGE_PCT", 0.0005) or 0)
    commission = float(getattr(config, "BT_COMMISSION_PCT", 0.001) or 0)

    # Quiet per-bar strategy logs during backtest
    strat_logger = logging.getLogger("strategy")
    prev_level = strat_logger.level
    strat_logger.setLevel(logging.WARNING)

    try:
        strategy = create_strategy(market=market, name=strategy_name)
        data = strategy.compute_indicators(df)

        equity = initial_equity
        peak = equity
        max_dd = 0.0
        curve = [equity]

        position = None
        wins = losses = 0
        gross_profit = gross_loss = 0.0
        trade_count = 0

        start_i = max(config.SMA_SLOW, 50) + 2

        for i in range(start_i, len(data)):
            window = data.iloc[: i + 1]
            bar = data.iloc[i]
            price = float(bar["close"])
            atr = float(bar["ATR"]) if not pd.isna(bar.get("ATR")) else None

            if position is not None:
                risk = position["entry"] - position["initial_stop"]
                peak_px = max(position.get("peak", position["entry"]), price)
                position["peak"] = peak_px
                if risk > 0 and (peak_px - position["entry"]) >= risk and atr and atr > 0:
                    trail = peak_px - (config.ATR_TRAIL_MULT * atr)
                    position["stop"] = max(position["stop"], trail)

                exit_px = None
                low = float(bar["low"])
                high = float(bar["high"])
                if low <= position["stop"]:
                    exit_px = position["stop"] * (1 - slip)
                elif high >= position["tp"]:
                    exit_px = position["tp"] * (1 - slip)
                else:
                    sig = strategy.generate_signal(window, symbol)
                    if sig == "SELL":
                        exit_px = price * (1 - slip)

                if exit_px is not None:
                    pnl = (exit_px - position["entry"]) * position["qty"]
                    pnl -= abs(position["entry"] * position["qty"]) * commission
                    equity += pnl
                    trade_count += 1
                    if pnl >= 0:
                        wins += 1
                        gross_profit += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    position = None

            else:
                sig = strategy.generate_signal(window, symbol)
                if sig == "BUY" and atr and atr > 0:
                    fill = price * (1 + slip)
                    stop = fill - atr_mult * atr
                    stop_dist = fill - stop
                    if stop_dist <= 0:
                        continue
                    risk_budget = equity * risk_pct
                    qty = int(risk_budget // stop_dist)
                    max_by_pct = int((equity * config.MAX_POSITION_PCT) // fill)
                    qty = min(qty, max_by_pct, config.MAX_SHARES_PER_ORDER)
                    if qty <= 0:
                        continue
                    tp = fill + tp_r * stop_dist
                    position = {
                        "qty": qty,
                        "entry": fill,
                        "stop": stop,
                        "initial_stop": stop,
                        "tp": tp,
                        "atr": atr,
                        "peak": fill,
                    }

            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
            curve.append(equity)

        if position is not None:
            price = float(data.iloc[-1]["close"])
            pnl = (price - position["entry"]) * position["qty"]
            equity += pnl
            trade_count += 1
            if pnl >= 0:
                wins += 1
                gross_profit += pnl
            else:
                losses += 1
                gross_loss += abs(pnl)
            curve.append(equity)

        pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        return BacktestResult(
            strategy=strategy_name,
            symbol=symbol,
            trades=trade_count,
            wins=wins,
            losses=losses,
            net_pnl=round(equity - initial_equity, 2),
            win_rate=round(wins / trade_count, 4) if trade_count else 0.0,
            profit_factor=round(pf, 4),
            max_drawdown=round(max_dd, 4),
            equity_curve=curve,
        )
    finally:
        strat_logger.setLevel(prev_level)


def print_result(r: BacktestResult):
    print(
        f"\n=== {r.strategy} @ {r.symbol} ===\n"
        f"  Trades:        {r.trades}\n"
        f"  Win rate:      {r.win_rate:.1%}\n"
        f"  Profit factor: {r.profit_factor:.2f}\n"
        f"  Net PnL:       {r.net_pnl:+,.2f}\n"
        f"  Max drawdown:  {r.max_drawdown:.1%}\n"
    )


def print_leaderboard(results: list[BacktestResult]):
    """Aggregate by strategy across symbols."""
    by_name: dict[str, list[BacktestResult]] = {}
    for r in results:
        by_name.setdefault(r.strategy, []).append(r)

    rows = []
    for name, items in by_name.items():
        trades = sum(i.trades for i in items)
        wins = sum(i.wins for i in items)
        net = sum(i.net_pnl for i in items)
        avg_dd = float(np.mean([i.max_drawdown for i in items])) if items else 0.0
        # Aggregate PF approx from per-symbol is imperfect; use median PF when trades exist
        pfs = [i.profit_factor for i in items if i.trades > 0]
        med_pf = float(np.median(pfs)) if pfs else 0.0
        wr = (wins / trades) if trades else 0.0
        rows.append((name, trades, wr, med_pf, net, avg_dd))

    rows.sort(key=lambda x: x[4], reverse=True)
    print("\n========== STRATEGY LEADERBOARD (sum across symbols) ==========")
    print(f"{'Strategy':<18} {'Trades':>7} {'Win%':>8} {'Med PF':>8} {'Net PnL':>12} {'Avg DD':>8}")
    print("-" * 70)
    for name, trades, wr, med_pf, net, avg_dd in rows:
        print(f"{name:<18} {trades:>7} {wr:>7.1%} {med_pf:>8.2f} {net:>+12,.2f} {avg_dd:>7.1%}")
    if rows:
        print(f"\nLeader by net PnL: {rows[0][0]}")
    print("===============================================================\n")
    return rows[0][0] if rows else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backtest pluggable strategies")
    parser.add_argument("--market", default="INDIA", choices=["INDIA", "FNO"])
    parser.add_argument(
        "--strategy",
        default=config.STRATEGY_NAME,
        help="trend_pullback | mean_reversion | regime_adaptive",
    )
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated list for multi-symbol compare (overrides --symbol)",
    )
    parser.add_argument("--timeframe", default="1Day", help="1Day or 1Hour")
    parser.add_argument("--lookback", type=int, default=500)
    parser.add_argument("--csv", default=None, help="OHLCV CSV path")
    parser.add_argument("--compare", action="store_true", help="Compare all built-in strategies")
    parser.add_argument("--demo", action="store_true", help="Use synthetic bars")
    parser.add_argument(
        "--dhan",
        action="store_true",
        help="Fetch Dhan historical OHLC (+ OI when API returns it) for equities/F&O underlyings",
    )
    parser.add_argument("--equity", type=float, default=100_000.0)
    args = parser.parse_args(argv)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = [args.symbol.upper()]

    # F&O market defaults to index underlyings for OHLC/OI fetch
    if args.market == "FNO" and args.symbol == "RELIANCE" and not args.symbols:
        symbols = list(getattr(config, "INDIA_FNO_UNIVERSE", ["NIFTY", "BANKNIFTY", "FINNIFTY"]))

    strategies = list(COMPARE_STRATEGIES) if args.compare else [args.strategy]
    results: list[BacktestResult] = []

    for symbol in symbols:
        if args.csv and len(symbols) == 1:
            df = load_csv(args.csv)
        elif args.dhan:
            try:
                from dhan_broker import get_shared_dhan_broker

                b = get_shared_dhan_broker(auto_login=True)
                # Longer lookback for F&O underlyings / equities
                days = min(max(args.lookback, 30), 90 if args.market == "FNO" else 300)
                df = b.get_historical_candles(symbol, timeframe=args.timeframe, days=days)
                if df is not None and "oi" in df.columns:
                    logger.info(f"{symbol}: Dhan OHLC+OI bars loaded (oi col present)")
                elif df is not None:
                    logger.info(f"{symbol}: Dhan OHLC bars loaded (OI not in response)")
            except Exception as e:
                logger.warning(f"Dhan historical candles fetch failed for {symbol}: {e}")
                df = None
            if df is None or df.empty:
                logger.info(f"{symbol}: Dhan data unavailable — falling back to synthetic bars")
                df = synthetic_bars(seed=abs(hash(symbol)) % 10_000)
        else:
            logger.info(f"{symbol}: using synthetic demo bars")
            df = synthetic_bars(seed=abs(hash(symbol)) % 10_000)

        logger.info(f"{symbol}: {len(df)} bars loaded — testing {strategies}")
        for name in strategies:
            r = run_backtest(
                df,
                strategy_name=name,
                market=args.market,
                symbol=symbol,
                initial_equity=args.equity,
            )
            print_result(r)
            results.append(r)

    winner = None
    if args.compare and results:
        winner = print_leaderboard(results)
    elif len(results) == 2:
        a, b = results
        better = a if a.net_pnl >= b.net_pnl else b
        print(
            f"Higher net PnL: {better.strategy} "
            f"({better.net_pnl:+,.2f} vs "
            f"{(b if better is a else a).net_pnl:+,.2f})"
        )
        winner = better.strategy

    if winner:
        print(f"Suggested STRATEGY_NAME={winner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
