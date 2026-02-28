"""データ取得モジュール。ccxt を用いて取引所からリアルタイム・過去データを取得する。"""

from __future__ import annotations

from typing import Any, Optional

import ccxt

from config import Config


class DataFetcher:
    """取引所APIから市場データを取得するクラス。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        exchange_class = getattr(ccxt, config.exchange, ccxt.bybit)
        self.exchange: ccxt.Exchange = exchange_class(
            {
                "apiKey": config.api_key or None,
                "secret": config.api_secret or None,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        self._markets_loaded = False
        self._perpetual_exchange: Optional[ccxt.Exchange] = None

    def _ensure_markets_loaded(self) -> None:
        """マーケット情報を読み込む。"""
        if not self._markets_loaded:
            self.exchange.load_markets()
            self._markets_loaded = True

    def _get_perpetual_exchange(self) -> ccxt.Exchange:
        """先物用の exchange インスタンスを取得する。"""
        if self._perpetual_exchange is None:
            exchange_class = getattr(ccxt, self.config.exchange, ccxt.bybit)
            self._perpetual_exchange = exchange_class(
                {
                    "apiKey": self.config.api_key or None,
                    "secret": self.config.api_secret or None,
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap"},
                }
            )
            self._perpetual_exchange.load_markets()
        return self._perpetual_exchange

    def get_tradable_symbols(self) -> list[str]:
        """現物と無期限先物の両方で上場している銘柄リストを返す。"""
        self._ensure_markets_loaded()
        perp = self._get_perpetual_exchange()

        spot_symbols: set[str] = set()
        for m in self.exchange.markets.values():
            if m.get("spot") and m.get("active", True):
                base_quote = f"{m['base']}/{m['quote']}"
                spot_symbols.add(base_quote)

        perpetual_symbols: set[str] = set()
        for m in perp.markets.values():
            if m.get("swap") and m.get("linear") and m.get("active", True):
                base_quote = f"{m['base']}/{m['quote']}"
                perpetual_symbols.add(base_quote)

        common = sorted(spot_symbols & perpetual_symbols)
        return common

    def get_funding_rates(self, symbols: list[str]) -> dict[str, float]:
        """各銘柄の現在のファンディングレートを取得する。"""
        perp = self._get_perpetual_exchange()
        result: dict[str, float] = {}

        for symbol in symbols:
            try:
                perp_symbol = self._to_perpetual_symbol(symbol)
                if perp_symbol is None:
                    continue
                fr = perp.fetch_funding_rate(perp_symbol)
                result[symbol] = float(fr.get("fundingRate", 0))
            except Exception:
                continue

        return result

    def _to_perpetual_symbol(self, symbol: str) -> Optional[str]:
        """現物シンボルを先物シンボルに変換する。Bybit/Binance は BASE/QUOTE:QUOTE 形式を要求。"""
        perp = self._get_perpetual_exchange()
        if "/" in symbol and ":" not in symbol:
            base, quote = symbol.split("/", 1)
            candidate = f"{base}/{quote}:{quote}"
            if candidate in perp.markets:
                return candidate
        if symbol in perp.markets:
            m = perp.markets[symbol]
            if m.get("swap") and m.get("linear"):
                return symbol
        for m in perp.markets.values():
            if m.get("swap") and m.get("linear"):
                if f"{m['base']}/{m['quote']}" == symbol:
                    return m["symbol"]
        return None

    def get_tickers(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """価格・出来高情報を取得する。"""
        self._ensure_markets_loaded()
        result: dict[str, dict[str, Any]] = {}

        for symbol in symbols:
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                result[symbol] = {
                    "last": float(ticker.get("last", 0)),
                    "volume": float(ticker.get("baseVolume", 0)),
                    "quoteVolume": float(ticker.get("quoteVolume", 0)),
                }
            except Exception:
                continue

        return result

    def get_orderbook(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        """スプレッド確認用のオーダーブックを取得する。"""
        self._ensure_markets_loaded()
        ob = self.exchange.fetch_order_book(symbol, limit)
        return {
            "bids": ob.get("bids", [])[:limit],
            "asks": ob.get("asks", [])[:limit],
        }

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: Optional[int] = 200,
    ) -> list[list]:
        """過去のOHLCVデータを取得する。"""
        self._ensure_markets_loaded()
        return self.exchange.fetch_ohlcv(symbol, timeframe, since, limit)

    def fetch_funding_rate_history(
        self,
        symbol: str,
        since: Optional[int] = None,
        limit: Optional[int] = 200,
    ) -> list[dict[str, Any]]:
        """過去のファンディングレート履歴を取得する。"""
        perp = self._get_perpetual_exchange()
        perp_symbol = self._to_perpetual_symbol(symbol)
        if perp_symbol is None:
            return []

        try:
            if hasattr(perp, "fetch_funding_rate_history"):
                return perp.fetch_funding_rate_history(
                    perp_symbol, since=since, limit=limit
                )
        except Exception:
            pass

        return []

    def get_market_data(self) -> dict[str, Any]:
        """Step 1 用: 銘柄一覧、FR、ティッカー、オーダーブックを一括取得する。"""
        symbols = self.get_tradable_symbols()
        funding_rates = self.get_funding_rates(symbols)
        tickers = self.get_tickers(symbols)

        market_data: list[dict[str, Any]] = []
        for symbol in symbols:
            if symbol not in funding_rates or symbol not in tickers:
                continue
            fr = funding_rates[symbol]
            tk = tickers[symbol]
            market_data.append(
                {
                    "symbol": symbol,
                    "funding_rate": fr,
                    "price": tk["last"],
                    "volume_24h": tk["volume"],
                    "quote_volume_24h": tk["quoteVolume"],
                }
            )

        return {"symbols": symbols, "market_data": market_data}
