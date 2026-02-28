"""メインエントリーポイント。モードに応じて LIVE/DRY_RUN ループまたはバックテストを実行する。"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import pandas as pd

from config import Config, Mode
from fetcher import DataFetcher
from screener import PairTradeScreener, calc_z_score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

LOOP_INTERVAL_SEC = 3600  # 1時間ごと
ROLLING_WINDOW = 200


def _fetch_and_calc(fetcher: DataFetcher, pair_symbols: list[str]) -> Optional[dict[str, Any]]:
    """OHLCV を取得し Z-Score を計算する。失敗時は None。"""
    end_ms = int(time.time() * 1000)
    since_ms = end_ms - (ROLLING_WINDOW + 50) * 3600 * 1000
    btc_sym = pair_symbols[0]  # BTC/USDT or BTC/JPY
    eth_sym = pair_symbols[1]  # ETH/USDT or ETH/JPY

    btc_ohlcv = fetcher.fetch_ohlcv_range(
        btc_sym, since_ms, end_ms, "1h", min_candles=ROLLING_WINDOW + 50
    )
    time.sleep(0.2)
    eth_ohlcv = fetcher.fetch_ohlcv_range(
        eth_sym, since_ms, end_ms, "1h", min_candles=ROLLING_WINDOW + 50
    )

    if not btc_ohlcv or not eth_ohlcv:
        detail = f"btc={len(btc_ohlcv or [])}, eth={len(eth_ohlcv or [])}"
        logger.warning("OHLCV取得失敗。%s", detail)
        return {"_fetch_failed": True, "reason": "ohlcv_empty", "detail": detail}

    btc_df = (
        pd.DataFrame(btc_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
    )
    eth_df = (
        pd.DataFrame(eth_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
    )
    merged = pd.merge(
        btc_df[["timestamp", "close"]].rename(columns={"close": "btc_close"}),
        eth_df[["timestamp", "close"]].rename(columns={"close": "eth_close"}),
        on="timestamp",
        how="inner",
    )
    merged = merged.sort_values("timestamp").tail(ROLLING_WINDOW + 1).reset_index(drop=True)

    if len(merged) < ROLLING_WINDOW + 1:
        detail = f"merged={len(merged)}, need={ROLLING_WINDOW + 1}"
        logger.warning("データ不足（%d件）。", len(merged))
        return {"_fetch_failed": True, "reason": "data_insufficient", "detail": detail}

    ratio, _, _, z_score = calc_z_score(
        merged["eth_close"], merged["btc_close"], window=ROLLING_WINDOW
    )
    z = float(z_score.iloc[-1])
    r = float(ratio.iloc[-1])
    eth_price = float(merged["eth_close"].iloc[-1])
    btc_price = float(merged["btc_close"].iloc[-1])

    if len(merged) >= 25:
        eth_24h = merged["eth_close"].iloc[-25]
        btc_24h = merged["btc_close"].iloc[-25]
        eth_change_24h = (eth_price - eth_24h) / eth_24h * 100 if eth_24h else None
        btc_change_24h = (btc_price - btc_24h) / btc_24h * 100 if btc_24h else None
    else:
        eth_change_24h = btc_change_24h = None

    return {
        "z_score": z,
        "ratio": r,
        "eth_price": eth_price,
        "btc_price": btc_price,
        "eth_change_24h": eth_change_24h,
        "btc_change_24h": btc_change_24h,
    }


def run_once(
    config: Config,
    storage: Optional[Any] = None,
    notifier: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """1回分の監視・取引ロジック。DRY_RUN/LIVE 共通のステートマシン。

    Returns:
        結果 dict。データ取得失敗時は None。
    """
    fetcher = DataFetcher(config)
    screener = PairTradeScreener(config, fetcher=fetcher)

    # 1. ポジション状態を読み込み
    state: dict[str, Any] = {"status": "NO_POSITION"}
    if config.mode == Mode.LIVE and storage:
        loaded = storage.load_position_state()
        if loaded:
            state = loaded

    # 2. OHLCV 取得 → Z-Score 計算
    data = _fetch_and_calc(fetcher, config.pair_symbols)
    if data is None or data.get("_fetch_failed"):
        return data

    z = data["z_score"]
    r = data["ratio"]
    eth_price = data["eth_price"]
    btc_price = data["btc_price"]
    eth_change_24h = data.get("eth_change_24h")
    btc_change_24h = data.get("btc_change_24h")

    result: dict[str, Any] = {
        "z_score": z,
        "ratio": r,
        "eth_price": eth_price,
        "btc_price": btc_price,
        "eth_change_24h": eth_change_24h,
        "btc_change_24h": btc_change_24h,
        "position_status": state.get("status", "NO_POSITION"),
        "signal_triggered": False,
        "trade_executed": False,
        "ai_decision": None,
        "ai_confidence": None,
        "ai_reason": None,
        "news_titles": [],
    }

    logger.info(
        "[%s] Z-Score=%.2f | Ratio=%.4f | ETH=%.2f BTC=%.2f | 24h: ETH %+.2f%% BTC %+.2f%%",
        config.mode.value,
        z, r, eth_price, btc_price,
        eth_change_24h or 0, btc_change_24h or 0,
    )

    # 3. ステートマシン
    if state["status"] == "NO_POSITION":
        should_enter, direction = screener.check_entry_signal(z)
        if should_enter:
            result["signal_triggered"] = True
            news_titles = fetcher.fetch_crypto_news(limit=10)
            result["news_titles"] = news_titles

            logger.info("[%s] シグナル検出: direction=%s | ニュース %d件", config.mode.value, direction, len(news_titles))
            for i, t in enumerate(news_titles[:5], 1):
                logger.info("  %d. %s", i, (t[:80] + "...") if len(t) > 80 else t)

            use_mock = not bool(config.deepseek_api_key)
            ai_result = screener.ai_decision(
                z, r, eth_price, btc_price,
                use_mock=use_mock,
                eth_change_24h_pct=eth_change_24h,
                btc_change_24h_pct=btc_change_24h,
                news_titles=news_titles,
            )
            decision = ai_result.get("decision", "PASS")
            confidence = ai_result.get("confidence", 0)
            reason = ai_result.get("reason", "")

            result["ai_decision"] = decision
            result["ai_confidence"] = confidence
            result["ai_reason"] = reason

            logger.info("[AI] decision=%s confidence=%d | REASON: %s", decision, confidence, reason or "(なし)")

            if decision == "ENTRY" and confidence > 70:
                if config.mode == Mode.LIVE:
                    from executor import Executor

                    executor = Executor(config, fetcher)
                    capital = config.initial_capital
                    ok = executor.open_pair_trade(direction, eth_price, btc_price, capital)
                    if ok:
                        new_state = {
                            "status": "OPEN",
                            "direction": direction,
                            "entry_z_score": z,
                            "entry_ratio": r,
                            "eth_entry_price": eth_price,
                            "btc_entry_price": btc_price,
                            "position_size_usd": capital * 0.5,
                            "entry_timestamp": time.time(),
                        }
                        if storage:
                            storage.save_position_state(new_state)
                        result["trade_executed"] = True
                        if notifier:
                            notifier.send_entry_alert(result, new_state)
                else:
                    logger.info("[DRY_RUN] エントリー条件満たすが発注なし（シミュレーション）")
                    if notifier:
                        notifier.send_signal_alert(result)
            else:
                if notifier and result["signal_triggered"]:
                    notifier.send_signal_alert(result)

    elif state["status"] == "OPEN":
        direction = state.get("direction", "")
        should_exit = screener.check_exit_signal(z)
        should_stop = screener.check_stop_loss(z, direction)

        if should_exit or should_stop:
            exit_reason = "mean_reversion" if should_exit else "stop_loss"

            if config.mode == Mode.LIVE:
                from executor import Executor

                executor = Executor(config, fetcher)
                ok = executor.close_pair_trade(direction, eth_price, btc_price, state)
                if ok:
                    eth_entry = state.get("eth_entry_price", eth_price)
                    btc_entry = state.get("btc_entry_price", btc_price)
                    position_usd = state.get("position_size_usd", 0)
                    entry_ts = state.get("entry_timestamp", 0)
                    duration_hours = (time.time() - entry_ts) / 3600 if entry_ts else 0

                    # PnL 簡易計算（ペアトレード: 比率の変化で損益）
                    entry_ratio = state.get("entry_ratio", r)
                    cost_rate = 0.006  # 往復 0.6%
                    pnl_pct = 0.0
                    if direction == "long_eth_short_btc":
                        pnl_pct = (r - entry_ratio) / entry_ratio * 100 - cost_rate * 100
                    else:
                        pnl_pct = (entry_ratio - r) / entry_ratio * 100 - cost_rate * 100
                    pnl_usd = position_usd * (pnl_pct / 100) if position_usd else 0

                    trade_result = {
                        "direction": direction,
                        "entry_z": state.get("entry_z_score"),
                        "exit_z": z,
                        "eth_entry_price": eth_entry,
                        "btc_entry_price": btc_entry,
                        "eth_exit_price": eth_price,
                        "btc_exit_price": btc_price,
                        "pnl_usd": pnl_usd,
                        "pnl_pct": pnl_pct,
                        "exit_reason": exit_reason,
                        "duration_hours": duration_hours,
                    }
                    if storage:
                        storage.save_trade(trade_result)
                        storage.save_position_state({"status": "NO_POSITION"})
                    result["trade_executed"] = True
                    result["trade_result"] = trade_result
                    if notifier:
                        notifier.send_exit_alert(result, trade_result)
            else:
                logger.info("[DRY_RUN] エグジット条件満たすが決済なし（シミュレーション）")

    return result


def run_backtest(config: Config) -> None:
    """バックテストモードを実行する（ペアトレード戦略）。"""
    from backtester import Backtester

    fetcher = DataFetcher(config)
    backtester = Backtester(config, fetcher)

    use_ai = bool(config.deepseek_api_key)
    logger.info(
        "ペアトレードバックテスト: %s | 期間: %s 〜 %s | AI: %s",
        " & ".join(config.pair_symbols),
        config.backtest_start or "未設定",
        config.backtest_end or "未設定",
        "DeepSeek API（本物）" if use_ai else "モック（常にENTRY）",
    )

    merged_df = backtester.load_data(
        start=config.backtest_start,
        end=config.backtest_end,
    )
    if merged_df.empty:
        logger.warning("データが取得できませんでした。")
        return

    backtester.run(merged_df)
    metrics = backtester.calculate_metrics()

    print("\n=== バックテスト結果 ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    backtester.plot_equity_curve()
    backtester.export_summary()
    logger.info("エクイティカーブを equity_curve.png に保存しました。")
    logger.info("サマリーを backtest_summary.csv に保存しました。")


def run_dry_run_loop(config: Config) -> None:
    """DRY_RUN モード: 1時間ごとに run_once を実行する。"""
    logger.info(
        "DRY_RUN 開始: %s | 1時間ごとにZスコア監視 | AI: %s | ニュース: %s",
        config.exchange,
        "DeepSeek API" if config.deepseek_api_key else "未設定（PASS）",
        "CryptoPanic/RSS" if config.cryptopanic_api_key else "RSS",
    )

    while True:
        try:
            run_once(config, storage=None, notifier=None)
            time.sleep(LOOP_INTERVAL_SEC)
        except KeyboardInterrupt:
            logger.info("DRY_RUN を終了します。")
            break
        except Exception as e:
            logger.exception("ループエラー: %s", e)
            time.sleep(60)


def main() -> None:
    """メインエントリーポイント。"""
    config = Config.from_env()

    if config.mode == Mode.BACKTEST:
        run_backtest(config)
    elif config.mode == Mode.DRY_RUN:
        run_dry_run_loop(config)
    elif config.mode == Mode.LIVE:
        logger.warning("LIVE モードは Lambda デプロイで実行してください。ローカルでは DRY_RUN を使用します。")
        run_dry_run_loop(config)
    else:
        logger.warning("不明なモードです。DRY_RUN で仮想運用してください。")
        run_dry_run_loop(config)


if __name__ == "__main__":
    main()
