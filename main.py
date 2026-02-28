"""メインエントリーポイント。モードに応じて LIVE/DRY_RUN ループまたはバックテストを実行する。"""

from __future__ import annotations

import logging
import time

from backtester import Backtester
from config import Config, Mode
from fetcher import DataFetcher
from screener import Screener
from executor import Executor, Portfolio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

LOOP_INTERVAL_SEC = 3600


def run_backtest(config: Config) -> None:
    """バックテストモードを実行する。"""
    fetcher = DataFetcher(config)
    backtester = Backtester(config, fetcher)

    ohlcv_df, fr_history = backtester.load_data()
    if ohlcv_df.empty:
        logger.warning("データが取得できませんでした。")
        return

    backtester.run(ohlcv_df, fr_history)
    metrics = backtester.calculate_metrics()

    print("\n=== バックテスト結果 ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    backtester.plot_equity_curve()
    backtester.export_summary()
    logger.info("エクイティカーブを equity_curve.png に保存しました。")
    logger.info("サマリーを backtest_summary.csv に保存しました。")


def run_live_loop(config: Config) -> None:
    """LIVE または DRY_RUN モードの1時間ループを実行する。"""
    fetcher = DataFetcher(config)
    screener = Screener(config)
    portfolio = Portfolio(config.initial_capital)
    executor = Executor(config, fetcher, portfolio)

    while True:
        try:
            market_data = fetcher.get_market_data()
            funding_rates = {
                m["symbol"]: m["funding_rate"]
                for m in market_data.get("market_data", [])
            }

            executor.check_and_collect_funding(funding_rates, time.time())

            for symbol in list(executor.portfolio.positions.keys()):
                fr = funding_rates.get(symbol, 0)
                if fr < config.min_fr_threshold:
                    tickers = fetcher.get_tickers([symbol])
                    exit_price = tickers.get(symbol, {}).get("last", 0)
                    if exit_price > 0:
                        executor.close_delta_neutral(symbol, exit_price)

            selected = screener.run(market_data)
            current_positions = set(executor.portfolio.positions.keys())
            slots = config.max_positions - len(current_positions)

            for sym_data in selected:
                if slots <= 0:
                    break
                symbol = sym_data["symbol"]
                if symbol in current_positions:
                    continue

                price = sym_data["price"]
                amount = (config.initial_capital * 0.1) / price
                if amount <= 0:
                    continue

                if executor.open_delta_neutral(symbol, amount, price):
                    slots -= 1
                    current_positions.add(symbol)

            logger.info(
                "残高: %.2f, ポジション数: %d, FR収益累計: %.2f",
                executor.portfolio.balance,
                len(executor.portfolio.positions),
                executor.portfolio.total_funding_collected,
            )

        except KeyboardInterrupt:
            logger.info("終了します。")
            break
        except Exception as e:
            logger.exception("ループエラー: %s", e)

        time.sleep(LOOP_INTERVAL_SEC)


def main() -> None:
    """メインエントリーポイント。"""
    config = Config.from_env()

    if config.mode == Mode.BACKTEST:
        run_backtest(config)
    else:
        run_live_loop(config)


if __name__ == "__main__":
    main()
