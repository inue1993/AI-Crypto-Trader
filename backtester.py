"""バックテストモジュール。過去データを用いた戦略シミュレーションと結果出力。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import Config
from fetcher import DataFetcher
from screener import Screener


@dataclass
class BacktestResult:
    """バックテスト結果。"""

    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    total_funding_collected: float = 0
    total_costs: float = 0
    max_drawdown: float = 0
    initial_capital: float = 0


class Backtester:
    """バックテスト実行クラス。"""

    def __init__(self, config: Config, fetcher: DataFetcher) -> None:
        self.config = config
        self.fetcher = fetcher
        self.screener = Screener(config)
        self.result = BacktestResult(initial_capital=config.initial_capital)

    def load_data(
        self,
        symbols: Optional[list[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        data_dir: Optional[Path] = None,
    ) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
        """OHLCV と FR 履歴を取得または CSV から読み込む。"""
        symbols = symbols or self.config.backtest_symbols
        start = start or self.config.backtest_start or "2024-01-01"
        end = end or self.config.backtest_end or "2024-03-31"

        start_ts = int(pd.Timestamp(start).timestamp() * 1000)
        end_ts = int(pd.Timestamp(end).timestamp() * 1000)

        if data_dir and data_dir.exists():
            return self._load_from_csv(data_dir, symbols)

        ohlcv_all: list[list] = []
        fr_history: dict[str, pd.DataFrame] = {}

        for symbol in symbols:
            since = start_ts
            symbol_ohlcv: list[list] = []
            while since < end_ts:
                candles = self.fetcher.fetch_ohlcv(
                    symbol, "1h", since=since, limit=200
                )
                if not candles:
                    break
                symbol_ohlcv.extend(candles)
                since = candles[-1][0] + 1
                time.sleep(0.2)

            if symbol_ohlcv:
                df = pd.DataFrame(
                    symbol_ohlcv,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["symbol"] = symbol
                ohlcv_all.extend(df.to_dict("records"))

            fr_data = self.fetcher.fetch_funding_rate_history(
                symbol, since=start_ts, limit=1000
            )
            if fr_data:
                fr_df = pd.DataFrame(
                    [
                        {
                            "timestamp": x["timestamp"],
                            "funding_rate": x.get("fundingRate", x.get("funding_rate", 0)),
                        }
                        for x in fr_data
                    ]
                )
                fr_history[symbol] = fr_df
            time.sleep(0.2)

        ohlcv_df = pd.DataFrame(ohlcv_all) if ohlcv_all else pd.DataFrame()

        if not ohlcv_df.empty and fr_history:
            valid = [s for s in fr_history if not fr_history[s].empty]
            if valid:
                min_fr_ts = min(
                    float(fr_history[s]["timestamp"].min()) for s in valid
                )
                before_len = len(ohlcv_df)
                ohlcv_df = ohlcv_df[ohlcv_df["timestamp"] >= min_fr_ts].copy()
                if len(ohlcv_df) == 0 and before_len > 0:
                    ohlcv_df = pd.DataFrame(ohlcv_all)

        return ohlcv_df, fr_history

    def _load_from_csv(
        self, data_dir: Path, symbols: list[str]
    ) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
        """ローカル CSV からデータを読み込む。"""
        ohlcv_list: list[pd.DataFrame] = []
        fr_history: dict[str, pd.DataFrame] = {}

        for symbol in symbols:
            base = symbol.replace("/", "_")
            ohlcv_path = data_dir / f"{base}_ohlcv_1h.csv"
            fr_path = data_dir / f"{base}_funding.csv"

            if ohlcv_path.exists():
                df = pd.read_csv(ohlcv_path)
                df["symbol"] = symbol
                ohlcv_list.append(df)
            if fr_path.exists():
                fr_history[symbol] = pd.read_csv(fr_path)

        ohlcv_df = pd.concat(ohlcv_list, ignore_index=True) if ohlcv_list else pd.DataFrame()
        return ohlcv_df, fr_history

    def run(
        self,
        ohlcv_df: pd.DataFrame,
        fr_history: dict[str, pd.DataFrame],
    ) -> BacktestResult:
        """時系列でシミュレーションを実行する。"""
        if ohlcv_df.empty:
            self.result.equity_curve = [self.config.initial_capital]
            self.result.timestamps = [0]
            return self.result

        balance = self.config.initial_capital
        positions: dict[str, dict[str, Any]] = {}
        equity_curve: list[float] = []
        timestamps: list[float] = []
        total_funding = 0.0
        total_costs = 0.0
        winning = 0
        total_trades = 0

        taker_fee = self.config.taker_fee
        slippage = self.config.slippage
        min_fr = self.config.min_fr_threshold
        max_positions = self.config.max_positions
        funding_interval_ms = 8 * 3600 * 1000

        timestamps_sorted = sorted(ohlcv_df["timestamp"].unique())

        for ts in timestamps_sorted:
            row = ohlcv_df[ohlcv_df["timestamp"] == ts]
            market_data = self._build_market_data_at_ts(row, fr_history, ts)
            candidates = self.screener.screen({"market_data": market_data})
            selected = self.screener.select_top(candidates)

            for sym_data in selected:
                symbol = sym_data["symbol"]
                if symbol in positions or len(positions) >= max_positions:
                    continue

                price = sym_data["price"]
                amount = (balance * 0.1) / price
                if amount <= 0:
                    continue

                cost = amount * price * 2 * (taker_fee + slippage)
                if cost > balance:
                    continue

                balance -= cost
                total_costs += cost
                positions[symbol] = {
                    "amount": amount,
                    "entry_price": price,
                    "entry_ts": ts,
                    "last_funding_ts": ts,
                }

            for symbol in list(positions.keys()):
                pos = positions[symbol]
                fr = self._get_fr_at_ts(symbol, ts, fr_history)
                elapsed = ts - pos["last_funding_ts"]
                if elapsed >= funding_interval_ms:
                    notional = pos["amount"] * pos["entry_price"]
                    funding = notional * fr
                    balance += funding
                    total_funding += funding
                    pos["last_funding_ts"] = ts

                if fr < min_fr:
                    exit_price = self._get_price_at_ts(symbol, ts, ohlcv_df)
                    exit_cost = pos["amount"] * exit_price * 2 * (taker_fee + slippage)
                    balance -= exit_cost
                    total_costs += exit_cost
                    pnl = pos["amount"] * (exit_price - pos["entry_price"]) * 2
                    if pnl > 0:
                        winning += 1
                    total_trades += 1
                    del positions[symbol]

            equity = balance + sum(
                p["amount"] * self._get_price_at_ts(s, ts, ohlcv_df)
                for s, p in positions.items()
            )
            equity_curve.append(equity)
            timestamps.append(ts)

        for symbol, pos in list(positions.items()):
            ts = timestamps_sorted[-1] if timestamps_sorted else 0
            exit_price = self._get_price_at_ts(symbol, ts, ohlcv_df)
            exit_cost = pos["amount"] * exit_price * 2 * (taker_fee + slippage)
            total_costs += exit_cost
            total_trades += 1

        peak = self.config.initial_capital
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        self.result = BacktestResult(
            equity_curve=equity_curve,
            timestamps=timestamps,
            total_trades=total_trades,
            winning_trades=winning,
            total_funding_collected=total_funding,
            total_costs=total_costs,
            max_drawdown=max_dd,
            initial_capital=self.config.initial_capital,
        )
        return self.result

    def _build_market_data_at_ts(
        self,
        row: pd.DataFrame,
        fr_history: dict[str, pd.DataFrame],
        ts: float,
    ) -> list[dict[str, Any]]:
        """指定タイムスタンプ時点の market_data を構築する。"""
        result: list[dict[str, Any]] = []
        for _, r in row.iterrows():
            symbol = r["symbol"]
            fr = self._get_fr_at_ts(symbol, ts, fr_history)
            result.append(
                {
                    "symbol": symbol,
                    "funding_rate": fr,
                    "price": r["close"],
                    "volume_24h": r.get("volume", 0),
                    "quote_volume_24h": r["close"] * r.get("volume", 0),
                }
            )
        return result

    def _get_fr_at_ts(
        self, symbol: str, ts: float, fr_history: dict[str, pd.DataFrame]
    ) -> float:
        """指定タイムスタンプ時点の FR を取得する。"""
        if symbol not in fr_history:
            return 0.0
        df = fr_history[symbol]
        if df.empty:
            return 0.0
        past = df[df["timestamp"] <= ts]
        if not past.empty:
            return float(past.iloc[-1]["funding_rate"])
        future = df[df["timestamp"] > ts]
        if not future.empty:
            return float(future.iloc[0]["funding_rate"])
        return 0.0

    def _get_price_at_ts(
        self, symbol: str, ts: float, ohlcv_df: pd.DataFrame
    ) -> float:
        """指定タイムスタンプ時点の価格を取得する。"""
        sub = ohlcv_df[(ohlcv_df["symbol"] == symbol) & (ohlcv_df["timestamp"] <= ts)]
        if sub.empty:
            return 0.0
        return float(sub.iloc[-1]["close"])

    def calculate_metrics(self) -> dict[str, Any]:
        """勝率、最大DD、累積FR収益、総コスト等を計算する。"""
        r = self.result
        win_rate = r.winning_trades / r.total_trades if r.total_trades > 0 else 0
        return {
            "total_trades": r.total_trades,
            "win_rate": win_rate,
            "max_drawdown": r.max_drawdown,
            "total_funding_collected": r.total_funding_collected,
            "total_costs": r.total_costs,
            "final_equity": r.equity_curve[-1] if r.equity_curve else r.initial_capital,
            "total_return_pct": (
                (r.equity_curve[-1] - r.initial_capital) / r.initial_capital * 100
                if r.equity_curve
                else 0
            ),
        }

    def plot_equity_curve(self, save_path: Optional[Path] = None) -> None:
        """エクイティカーブを matplotlib で描画する。"""
        r = self.result
        if not r.equity_curve:
            return

        timestamps = pd.to_datetime(np.array(r.timestamps), unit="ms")
        plt.figure(figsize=(10, 6))
        plt.plot(timestamps, r.equity_curve)
        plt.xlabel("Time")
        plt.ylabel("Equity (USD)")
        plt.title("Backtest Equity Curve")
        plt.grid(True)
        plt.tight_layout()

        if save_path:
            plt.savefig(save_path)
        else:
            plt.savefig("equity_curve.png")
        plt.close()

    def export_summary(self, filepath: str | Path = "backtest_summary.csv") -> None:
        """サマリーを CSV で出力する。"""
        metrics = self.calculate_metrics()
        df = pd.DataFrame([metrics])
        df.to_csv(filepath, index=False)
