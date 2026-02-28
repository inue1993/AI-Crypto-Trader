"""ペアトレードバックテストモジュール。Z-Scoreベースの平均回帰戦略のシミュレーション。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import Config
from fetcher import DataFetcher
from screener import (
    PairTradeScreener,
    calc_z_score,
    MIN_CONFIDENCE_FOR_ENTRY,
    Z_SCORE_EXIT_THRESHOLD,
    Z_SCORE_STOP_LOSS,
)

logger = logging.getLogger(__name__)

# デフォルト銘柄（config.pair_symbols で上書き）
PAIR_SYMBOLS = ["BTC/USDT", "ETH/USDT"]

# 取引コスト: 0.15% per side（エントリー・エグジットそれぞれ）
TRANSACTION_COST_RATE = 0.0015

# ポジションサイズ: 資金の50%（各銘柄25%ずつ）
POSITION_SIZE_PCT = 0.5

# Z-Score 閾値
Z_ENTRY = 2.0

# Rolling window
ROLLING_WINDOW = 200


@dataclass
class BacktestResult:
    """バックテスト結果。"""

    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    z_scores: list[float] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    total_costs: float = 0
    max_drawdown: float = 0
    initial_capital: float = 0


class Backtester:
    """ペアトレードバックテスト実行クラス。"""

    def __init__(self, config: Config, fetcher: DataFetcher) -> None:
        self.config = config
        self.fetcher = fetcher
        self.screener = PairTradeScreener(config, fetcher=fetcher)
        self.result = BacktestResult(initial_capital=config.initial_capital)

    def load_data(
        self,
        symbols: Optional[list[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        data_dir: Optional[Path] = None,
    ) -> pd.DataFrame:
        """BTC/USDT と ETH/USDT の1時間足OHLCVを取得し、マージしたDataFrameを返す。

        期間指定: start/end で指定。ROLLING_WINDOW(200)分のウォームアップ用に、
        取得開始日は start の約10日前からとする。
        """
        symbols = symbols or self.config.pair_symbols
        start = start or self.config.backtest_start or "2024-01-01"
        end = end or self.config.backtest_end or "2024-03-31"

        start_dt = pd.Timestamp(start)
        end_dt = pd.Timestamp(end)
        # ウォームアップ: ROLLING_WINDOW(200h)分のため、約10日分前から取得
        fetch_start_dt = start_dt - pd.Timedelta(days=10)
        start_ts = int(fetch_start_dt.timestamp() * 1000)
        end_ts = int(end_dt.timestamp() * 1000)

        if data_dir and data_dir.exists():
            return self._load_from_csv(data_dir, symbols)

        merged: Optional[pd.DataFrame] = None

        for symbol in symbols:
            symbol_ohlcv = self.fetcher.fetch_ohlcv_range(
                symbol,
                start_ts,
                end_ts,
                "1h",
                min_candles=ROLLING_WINDOW + 50,
            )

            if not symbol_ohlcv:
                continue

            df = pd.DataFrame(
                symbol_ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df = df.rename(columns={"close": f"close_{symbol.replace('/', '_')}"})
            df = df[["timestamp", f"close_{symbol.replace('/', '_')}"]]
            df = df.drop_duplicates(subset=["timestamp"])

            if merged is None:
                merged = df
            else:
                merged = pd.merge(
                    merged, df, on="timestamp", how="outer", sort=True
                )
            time.sleep(0.2)

        if merged is None or merged.empty:
            return pd.DataFrame()

        merged = merged.sort_values("timestamp").reset_index(drop=True)
        merged = merged.dropna()
        return merged

    def _load_from_csv(
        self, data_dir: Path, symbols: list[str]
    ) -> pd.DataFrame:
        """ローカルCSVからOHLCVを読み込み、マージする。"""
        dfs: list[pd.DataFrame] = []
        for symbol in symbols:
            base = symbol.replace("/", "_")
            path = data_dir / f"{base}_ohlcv_1h.csv"
            if path.exists():
                df = pd.read_csv(path)
                col = f"close_{symbol.replace('/', '_')}"
                if "close" in df.columns and col not in df.columns:
                    df = df.rename(columns={"close": col})
                if "timestamp" in df.columns and col in df.columns:
                    dfs.append(df[["timestamp", col]])
        if not dfs:
            return pd.DataFrame()
        merged = dfs[0]
        for df in dfs[1:]:
            merged = pd.merge(merged, df, on="timestamp", how="outer", sort=True)
        merged = merged.sort_values("timestamp").dropna().reset_index(drop=True)
        return merged

    def _get_eth_btc_columns(self, merged_df: pd.DataFrame) -> tuple[str, str]:
        """マージ済みDataFrameからETH/BTCの終値カラム名を取得する。"""
        eth_cols = [c for c in merged_df.columns if c.startswith("close_ETH")]
        btc_cols = [c for c in merged_df.columns if c.startswith("close_BTC")]
        eth_col = eth_cols[0] if eth_cols else "close_ETH_USDT"
        btc_col = btc_cols[0] if btc_cols else "close_BTC_USDT"
        return eth_col, btc_col

    def run(self, merged_df: pd.DataFrame) -> BacktestResult:
        """ペアトレード戦略の時系列シミュレーションを実行する。"""
        eth_col, btc_col = self._get_eth_btc_columns(merged_df)

        if merged_df.empty or eth_col not in merged_df.columns or btc_col not in merged_df.columns:
            self.result.equity_curve = [self.config.initial_capital]
            self.result.timestamps = [0]
            self.result.z_scores = [0.0]
            return self.result

        eth_closes = merged_df[eth_col]
        btc_closes = merged_df[btc_col]

        ratio, rolling_mean, rolling_std, z_score = calc_z_score(
            eth_closes, btc_closes, window=ROLLING_WINDOW
        )

        merged_df = merged_df.copy()
        merged_df["ratio"] = ratio
        merged_df["z_score"] = z_score

        balance = self.config.initial_capital
        position: Optional[dict[str, Any]] = None  # 1ポジションのみ
        equity_curve: list[float] = []
        timestamps: list[float] = []
        z_scores_out: list[float] = []
        total_costs = 0.0
        total_trades = 0
        winning_trades = 0

        for i in range(ROLLING_WINDOW, len(merged_df)):
            row = merged_df.iloc[i]
            ts = row["timestamp"]
            eth_price = row[eth_col]
            btc_price = row[btc_col]
            z = row["z_score"]
            r = row["ratio"]

            ts_str = pd.Timestamp(ts, unit="ms").strftime("%Y-%m-%d %H:%M")

            if position is not None:
                # ハード・ストップロス: Z-Score が ±3.5 を超えたら即座に損切り
                if self.screener.check_stop_loss(z, position["direction"]):
                    direction = position["direction"]
                    eth_notional = position["eth_notional"]
                    btc_notional = position["btc_notional"]

                    if direction == "long_eth_short_btc":
                        eth_pnl = position["eth_amount"] * (eth_price - position["eth_entry"])
                        btc_pnl = position["btc_amount"] * (position["btc_entry"] - btc_price)
                    else:
                        eth_pnl = position["eth_amount"] * (position["eth_entry"] - eth_price)
                        btc_pnl = position["btc_amount"] * (btc_price - position["btc_entry"])

                    trade_pnl = eth_pnl + btc_pnl
                    exit_cost = eth_notional * TRANSACTION_COST_RATE + btc_notional * TRANSACTION_COST_RATE
                    total_costs += exit_cost
                    balance += eth_notional + btc_notional + trade_pnl - exit_cost

                    total_trades += 1
                    net_pnl = trade_pnl - position["entry_cost"] - exit_cost
                    if net_pnl > 0:
                        winning_trades += 1

                    logger.info(
                        "[BT] EXIT(STOP_LOSS) ts=%s direction=%s | Z=%.2f | eth_pnl=%.4f btc_pnl=%.4f | net_pnl=%.4f",
                        ts_str, direction, z, eth_pnl, btc_pnl, net_pnl,
                    )
                    position = None
                # エグジット判定: Z-Score が 0 に戻ったか（利益確定）
                elif self.screener.check_exit_signal(z):
                    direction = position["direction"]
                    eth_notional = position["eth_notional"]
                    btc_notional = position["btc_notional"]

                    # エグジット時の価格で損益計算
                    eth_pnl = 0.0
                    btc_pnl = 0.0
                    if direction == "long_eth_short_btc":
                        eth_pnl = position["eth_amount"] * (eth_price - position["eth_entry"])
                        btc_pnl = position["btc_amount"] * (position["btc_entry"] - btc_price)
                    else:  # short_eth_long_btc
                        eth_pnl = position["eth_amount"] * (position["eth_entry"] - eth_price)
                        btc_pnl = position["btc_amount"] * (btc_price - position["btc_entry"])

                    trade_pnl = eth_pnl + btc_pnl

                    # エグジットコスト: 0.15% × 各銘柄
                    exit_cost = eth_notional * TRANSACTION_COST_RATE + btc_notional * TRANSACTION_COST_RATE
                    total_costs += exit_cost
                    balance += eth_notional + btc_notional + trade_pnl - exit_cost

                    total_trades += 1
                    net_pnl = trade_pnl - position["entry_cost"] - exit_cost
                    if net_pnl > 0:
                        winning_trades += 1

                    logger.info(
                        "[BT] EXIT ts=%s direction=%s | eth_pnl=%.4f btc_pnl=%.4f | entry_cost=%.4f exit_cost=%.4f | net_pnl=%.4f",
                        ts_str, direction, eth_pnl, btc_pnl,
                        position["entry_cost"], exit_cost, net_pnl,
                    )
                    position = None

            if position is None:
                # エントリー判定
                should_enter, direction = self.screener.check_entry_signal(z)
                if should_enter and direction:
                    # 直前24時間の騰落率を算出（AIプロンプト用）
                    eth_change_24h = None
                    btc_change_24h = None
                    ts_24h_ago = ts - 24 * 3600 * 1000
                    past = merged_df[merged_df["timestamp"] <= ts_24h_ago]
                    if not past.empty:
                        row_24h = past.iloc[-1]
                        eth_24h = row_24h[eth_col]
                        btc_24h = row_24h[btc_col]
                        if eth_24h and eth_24h > 0:
                            eth_change_24h = (eth_price - eth_24h) / eth_24h * 100
                        if btc_24h and btc_24h > 0:
                            btc_change_24h = (btc_price - btc_24h) / btc_24h * 100

                    # AI判定（DEEPSEEK_API_KEY 設定時は本物のAPI、未設定時はモック）
                    use_mock = not bool(self.config.deepseek_api_key)
                    ai_result = self.screener.ai_decision(
                        z, r, eth_price, btc_price, use_mock=use_mock,
                        eth_change_24h_pct=eth_change_24h,
                        btc_change_24h_pct=btc_change_24h,
                    )
                    decision = ai_result.get("decision", "PASS")
                    confidence = ai_result.get("confidence", 0)
                    reason = ai_result.get("reason", "")

                    logger.info(
                        "[AI] ts=%s Z=%.2f | decision=%s confidence=%d | REASON: %s",
                        ts_str, z, decision, confidence, reason or "(なし)",
                    )

                    if decision != "ENTRY" or confidence <= MIN_CONFIDENCE_FOR_ENTRY:
                        if decision == "ENTRY" and confidence <= MIN_CONFIDENCE_FOR_ENTRY:
                            logger.info(
                                "[AI] エントリー見送り: confidence %d <= %d（閾値未満）",
                                confidence, MIN_CONFIDENCE_FOR_ENTRY,
                            )
                        continue  # AI が PASS または confidence 不足の場合はスキップ

                    # エントリー許可（decision==ENTRY かつ confidence>70）
                    # ポジションサイズ: 資金の50%（各銘柄25%ずつ）
                    alloc = balance * POSITION_SIZE_PCT
                    eth_alloc = alloc / 2
                    btc_alloc = alloc / 2

                    eth_amount = eth_alloc / eth_price
                    btc_amount = btc_alloc / btc_price

                    eth_notional = eth_amount * eth_price
                    btc_notional = btc_amount * btc_price

                    # エントリーコスト: 0.15% × 各銘柄
                    entry_cost = eth_notional * TRANSACTION_COST_RATE + btc_notional * TRANSACTION_COST_RATE
                    total_required = eth_notional + btc_notional + entry_cost

                    if total_required <= balance:
                        balance -= total_required
                        total_costs += entry_cost

                        position = {
                            "direction": direction,
                            "eth_amount": eth_amount,
                            "btc_amount": btc_amount,
                            "eth_entry": eth_price,
                            "btc_entry": btc_price,
                            "eth_notional": eth_notional,
                            "btc_notional": btc_notional,
                            "entry_cost": entry_cost,
                            "entry_ts": ts,
                        }
                        logger.info(
                            "[BT] ENTRY ts=%s direction=%s | Z=%.2f | eth=%.4f @ %.2f btc=%.4f @ %.2f | cost=%.4f",
                            ts_str, direction, z, eth_amount, eth_price, btc_amount, btc_price, entry_cost,
                        )

            # エクイティ計算
            if position is not None:
                direction = position["direction"]
                if direction == "long_eth_short_btc":
                    eth_val = position["eth_amount"] * eth_price
                    btc_val = position["btc_amount"] * btc_price
                    eth_upl = position["eth_amount"] * (eth_price - position["eth_entry"])
                    btc_upl = position["btc_amount"] * (position["btc_entry"] - btc_price)
                else:
                    eth_val = position["eth_amount"] * eth_price  # ショートの評価
                    btc_val = position["btc_amount"] * btc_price
                    eth_upl = position["eth_amount"] * (position["eth_entry"] - eth_price)
                    btc_upl = position["btc_amount"] * (btc_price - position["btc_entry"])
                equity = balance + eth_val + btc_val + eth_upl + btc_upl
            else:
                equity = balance

            equity_curve.append(equity)
            timestamps.append(ts)
            z_scores_out.append(z if not pd.isna(z) else 0.0)

        # ポジションが残っている場合は最終価格で決済
        if position is not None:
            row = merged_df.iloc[-1]
            eth_price = row[eth_col]
            btc_price = row[btc_col]
            direction = position["direction"]
            eth_notional = position["eth_notional"]
            btc_notional = position["btc_notional"]

            if direction == "long_eth_short_btc":
                eth_pnl = position["eth_amount"] * (eth_price - position["eth_entry"])
                btc_pnl = position["btc_amount"] * (position["btc_entry"] - btc_price)
            else:
                eth_pnl = position["eth_amount"] * (position["eth_entry"] - eth_price)
                btc_pnl = position["btc_amount"] * (btc_price - position["btc_entry"])

            trade_pnl = eth_pnl + btc_pnl
            exit_cost = eth_notional * TRANSACTION_COST_RATE + btc_notional * TRANSACTION_COST_RATE
            total_costs += exit_cost
            balance += eth_notional + btc_notional + trade_pnl - exit_cost
            total_trades += 1
            net_pnl = trade_pnl - position["entry_cost"] - exit_cost
            if net_pnl > 0:
                winning_trades += 1
            logger.info(
                "[BT] EXIT(END) direction=%s | net_pnl=%.4f",
                direction, net_pnl,
            )

        # 最大ドローダウン
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
            z_scores=z_scores_out,
            total_trades=total_trades,
            winning_trades=winning_trades,
            total_costs=total_costs,
            max_drawdown=max_dd,
            initial_capital=self.config.initial_capital,
        )
        return self.result

    def calculate_metrics(self) -> dict[str, Any]:
        """勝率、最大DD、総コスト等を計算する。"""
        r = self.result
        win_rate = r.winning_trades / r.total_trades if r.total_trades > 0 else 0
        final_eq = r.equity_curve[-1] if r.equity_curve else r.initial_capital
        return {
            "total_trades": r.total_trades,
            "winning_trades": r.winning_trades,
            "win_rate": win_rate,
            "max_drawdown": r.max_drawdown,
            "total_costs": r.total_costs,
            "final_equity": final_eq,
            "total_return_pct": (
                (final_eq - r.initial_capital) / r.initial_capital * 100
                if r.equity_curve
                else 0
            ),
        }

    def plot_equity_curve(self, save_path: Optional[Path] = None) -> None:
        """Z-Scoreサブグラフとエクイティカーブを上下に並べて描画する。"""
        r = self.result
        if not r.equity_curve:
            return

        timestamps = pd.to_datetime(np.array(r.timestamps), unit="ms")

        fig, (ax2, ax1) = plt.subplots(2, 1, figsize=(10, 8), sharex=True, height_ratios=[1, 2])

        # メイン: エクイティカーブ
        ax1.plot(timestamps, r.equity_curve)
        currency = "JPY" if self.config.is_bitbank else "USD"
        ax1.set_ylabel(f"Equity ({currency})")
        ax1.set_title("Backtest Equity Curve")
        ax1.grid(True)

        # サブ: Z-Score 推移
        ax2.plot(timestamps, r.z_scores, color="steelblue", alpha=0.8)
        ax2.axhline(y=Z_ENTRY, color="green", linestyle="--", alpha=0.7, label=f"Entry ±{Z_ENTRY}")
        ax2.axhline(y=-Z_ENTRY, color="green", linestyle="--", alpha=0.7)
        ax2.axhline(y=Z_SCORE_STOP_LOSS, color="red", linestyle="-", alpha=0.8, label=f"Stop ±{Z_SCORE_STOP_LOSS}")
        ax2.axhline(y=-Z_SCORE_STOP_LOSS, color="red", linestyle="-", alpha=0.8)
        ax2.axhline(y=Z_SCORE_EXIT_THRESHOLD, color="orange", linestyle=":", alpha=0.6)
        ax2.axhline(y=-Z_SCORE_EXIT_THRESHOLD, color="orange", linestyle=":", alpha=0.6)
        ax2.axhline(y=0, color="gray", linestyle="-", alpha=0.5, label=f"Exit ±{Z_SCORE_EXIT_THRESHOLD}")
        ax2.set_ylabel("Z-Score")
        ax2.set_title("ETH/BTC Ratio Z-Score")
        ax2.grid(True)
        ax2.legend(loc="upper right", fontsize=8)

        plt.xlabel("Time")
        plt.tight_layout()

        if save_path:
            plt.savefig(save_path)
        else:
            plt.savefig("equity_curve.png")
        plt.close()

    def export_summary(self, filepath: str | Path = "backtest_summary.csv") -> None:
        """サマリーをCSVで出力する。"""
        metrics = self.calculate_metrics()
        df = pd.DataFrame([metrics])
        df.to_csv(filepath, index=False)
