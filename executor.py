"""注文執行・ポートフォリオ管理モジュール。LIVE/DRY_RUN での執行ロジックを実装する。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import ccxt

from config import Config, Mode
from fetcher import DataFetcher

logger = logging.getLogger(__name__)

FUNDING_INTERVAL_HOURS = 8


@dataclass
class Position:
    """保有ポジション情報。"""

    symbol: str
    amount: float
    entry_price_spot: float
    entry_price_perp: float
    entry_timestamp: float
    last_funding_timestamp: float


@dataclass
class Portfolio:
    """仮想/実ポートフォリオ管理。"""

    initial_capital: float
    balance: float = field(default=0)
    positions: dict[str, Position] = field(default_factory=dict)
    trade_history: list[dict[str, Any]] = field(default_factory=list)
    total_funding_collected: float = field(default=0)
    total_fees_paid: float = field(default=0)

    def __post_init__(self) -> None:
        self.balance = self.initial_capital

    def add_position(
        self,
        symbol: str,
        amount: float,
        entry_price_spot: float,
        entry_price_perp: float,
        timestamp: float,
        fee: float = 0,
    ) -> None:
        """ポジションを追加する。"""
        self.positions[symbol] = Position(
            symbol=symbol,
            amount=amount,
            entry_price_spot=entry_price_spot,
            entry_price_perp=entry_price_perp,
            entry_timestamp=timestamp,
            last_funding_timestamp=timestamp,
        )
        self.balance -= fee
        self.total_fees_paid += fee
        self.trade_history.append(
            {
                "action": "open",
                "symbol": symbol,
                "amount": amount,
                "price_spot": entry_price_spot,
                "price_perp": entry_price_perp,
                "fee": fee,
            }
        )

    def close_position(
        self,
        symbol: str,
        exit_price_spot: float,
        exit_price_perp: float,
        fee: float = 0,
    ) -> Optional[Position]:
        """ポジションを決済する。"""
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        self.balance -= fee
        self.total_fees_paid += fee
        self.trade_history.append(
            {
                "action": "close",
                "symbol": symbol,
                "amount": pos.amount,
                "exit_price_spot": exit_price_spot,
                "exit_price_perp": exit_price_perp,
                "fee": fee,
            }
        )
        return pos

    def record_funding(self, symbol: str, amount: float, timestamp: float) -> None:
        """FR収益を記録する。"""
        if symbol in self.positions:
            self.positions[symbol].last_funding_timestamp = timestamp
        self.total_funding_collected += amount
        self.balance += amount


class Executor:
    """注文執行クラス。DRY_RUN は仮想発注、LIVE は実発注。"""

    def __init__(
        self,
        config: Config,
        fetcher: DataFetcher,
        portfolio: Optional[Portfolio] = None,
    ) -> None:
        self.config = config
        self.fetcher = fetcher
        self.portfolio = portfolio or Portfolio(config.initial_capital)
        self._spot_exchange: Optional[ccxt.Exchange] = None
        self._perp_exchange: Optional[ccxt.Exchange] = None

    def _get_spot_exchange(self) -> ccxt.Exchange:
        if self._spot_exchange is None:
            ex_class = getattr(ccxt, self.config.exchange, ccxt.bybit)
            self._spot_exchange = ex_class(
                {
                    "apiKey": self.config.api_key,
                    "secret": self.config.api_secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                }
            )
            self._spot_exchange.load_markets()
        return self._spot_exchange

    def _get_perp_exchange(self) -> ccxt.Exchange:
        if self._perp_exchange is None:
            ex_class = getattr(ccxt, self.config.exchange, ccxt.bybit)
            self._perp_exchange = ex_class(
                {
                    "apiKey": self.config.api_key,
                    "secret": self.config.api_secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap"},
                }
            )
            self._perp_exchange.load_markets()
        return self._perp_exchange

    def _to_perp_symbol(self, symbol: str) -> Optional[str]:
        perp = self._get_perp_exchange()
        if symbol in perp.markets:
            return symbol
        if "/" in symbol and ":" not in symbol:
            base, quote = symbol.split("/", 1)
            candidate = f"{base}/{quote}:{quote}"
            if candidate in perp.markets:
                return candidate
        return None

    def open_delta_neutral(
        self, symbol: str, amount: float, price: float
    ) -> bool:
        """現物買いと先物売りを同時発注（デルタニュートラル構築）。"""
        if self.config.mode == Mode.DRY_RUN:
            return self._open_delta_neutral_dry_run(symbol, amount, price)
        return self._open_delta_neutral_live(symbol, amount, price)

    def _open_delta_neutral_dry_run(
        self, symbol: str, amount: float, price: float
    ) -> bool:
        slippage = self.config.slippage
        price_spot = price * (1 + slippage)
        price_perp = price * (1 - slippage)
        fee = amount * price * 2 * self.config.taker_fee

        self.portfolio.add_position(
            symbol=symbol,
            amount=amount,
            entry_price_spot=price_spot,
            entry_price_perp=price_perp,
            timestamp=time.time(),
            fee=fee,
        )
        logger.info(
            "【DRY RUN】%s: 現物仮想買い @%.2f, 先物仮想売り @%.2f",
            symbol,
            price_spot,
            price_perp,
        )
        return True

    def _open_delta_neutral_live(
        self, symbol: str, amount: float, price: float
    ) -> bool:
        spot = self._get_spot_exchange()
        perp = self._get_perp_exchange()
        perp_symbol = self._to_perp_symbol(symbol)
        if perp_symbol is None:
            logger.error("先物シンボルが見つかりません: %s", symbol)
            return False

        spot_order = None
        perp_order = None

        try:
            spot_order = spot.create_market_buy_order(symbol, amount)
            perp_order = perp.create_market_sell_order(perp_symbol, amount)
        except Exception as e:
            logger.error("片駆け発生: %s", e)
            if spot_order is not None and perp_order is None:
                self._emergency_close_spot(symbol, amount)
            elif spot_order is None and perp_order is not None:
                self._emergency_close_perp(perp_symbol, amount)
            return False

        price_spot = float(spot_order.get("average") or spot_order.get("price") or price)
        price_perp = float(perp_order.get("average") or perp_order.get("price") or price)
        fee = amount * (price_spot + price_perp) * self.config.taker_fee

        self.portfolio.add_position(
            symbol=symbol,
            amount=amount,
            entry_price_spot=price_spot,
            entry_price_perp=price_perp,
            timestamp=time.time(),
            fee=fee,
        )
        logger.info(
            "【LIVE】%s: 現物買い @%.2f, 先物売り @%.2f",
            symbol,
            price_spot,
            price_perp,
        )
        return True

    def _emergency_close_spot(self, symbol: str, amount: float) -> None:
        """片駆け時: 現物を即時売却。"""
        spot = self._get_spot_exchange()
        try:
            spot.create_market_sell_order(symbol, amount)
            logger.info("緊急決済: 現物 %s 売却", symbol)
        except Exception as e:
            logger.error("緊急決済失敗: %s", e)

    def _emergency_close_perp(self, perp_symbol: str, amount: float) -> None:
        """片駆け時: 先物を即時決済。"""
        perp = self._get_perp_exchange()
        try:
            perp.create_market_buy_order(perp_symbol, amount)
            logger.info("緊急決済: 先物 %s 決済", perp_symbol)
        except Exception as e:
            logger.error("緊急決済失敗: %s", e)

    def close_delta_neutral(self, symbol: str, exit_price: float) -> bool:
        """デルタニュートラルポジションを決済する。"""
        if symbol not in self.portfolio.positions:
            return False

        if self.config.mode == Mode.DRY_RUN:
            return self._close_delta_neutral_dry_run(symbol, exit_price)
        return self._close_delta_neutral_live(symbol, exit_price)

    def _close_delta_neutral_dry_run(
        self, symbol: str, exit_price: float
    ) -> bool:
        pos = self.portfolio.positions[symbol]
        slippage = self.config.slippage
        price_spot = exit_price * (1 - slippage)
        price_perp = exit_price * (1 + slippage)
        fee = pos.amount * exit_price * 2 * self.config.taker_fee

        self.portfolio.close_position(symbol, price_spot, price_perp, fee)
        logger.info("【DRY RUN】%s: 仮想決済 @%.2f", symbol, exit_price)
        return True

    def _close_delta_neutral_live(self, symbol: str, exit_price: float) -> bool:
        perp_symbol = self._to_perp_symbol(symbol)
        if perp_symbol is None:
            return False

        pos = self.portfolio.positions[symbol]
        spot = self._get_spot_exchange()
        perp = self._get_perp_exchange()

        spot_order = None
        perp_order = None

        try:
            spot_order = spot.create_market_sell_order(symbol, pos.amount)
            perp_order = perp.create_market_buy_order(perp_symbol, pos.amount)
        except Exception as e:
            logger.error("決済時片駆け: %s", e)
            if spot_order is not None and perp_order is None:
                self._emergency_close_spot(symbol, pos.amount)
            elif spot_order is None and perp_order is not None:
                self._emergency_close_perp(perp_symbol, pos.amount)
            return False

        price_spot = float(spot_order.get("average") or spot_order.get("price") or exit_price)
        price_perp = float(perp_order.get("average") or perp_order.get("price") or exit_price)
        fee = pos.amount * (price_spot + price_perp) * self.config.taker_fee

        self.portfolio.close_position(symbol, price_spot, price_perp, fee)
        logger.info("【LIVE】%s: 決済完了", symbol)
        return True

    def check_and_collect_funding(
        self, funding_rates: dict[str, float], current_timestamp: float
    ) -> None:
        """FR支払いタイミングを監視し、該当ポジションにFR収益を計上する。"""
        for symbol, pos in list(self.portfolio.positions.items()):
            fr = funding_rates.get(symbol, 0)
            elapsed = current_timestamp - pos.last_funding_timestamp
            interval_sec = FUNDING_INTERVAL_HOURS * 3600

            if elapsed >= interval_sec:
                notional = pos.amount * (
                    (pos.entry_price_spot + pos.entry_price_perp) / 2
                )
                funding_amount = notional * fr
                self.portfolio.record_funding(symbol, funding_amount, current_timestamp)
                logger.info(
                    "FR収益計上 %s: %.4f (FR=%.6f)",
                    symbol,
                    funding_amount,
                    fr,
                )
