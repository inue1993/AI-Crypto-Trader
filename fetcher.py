"""データ取得モジュール。ccxt を用いて取引所からリアルタイム・過去データを取得する。"""

from __future__ import annotations

import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional

import ccxt

from config import Config

logger = logging.getLogger(__name__)

# 暗号通貨ニュース取得
CRYPTOPANIC_API_URL = "https://cryptopanic.com/api/developer/v2/posts/"
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
NEWS_KEYWORDS = re.compile(r"\b(bitcoin|btc|ethereum|eth)\b", re.I)


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

    def _get_perpetual_exchange(self) -> Optional[ccxt.Exchange]:
        """先物用の exchange インスタンスを取得する。bitbank は先物非対応のため None。"""
        if self.config.is_bitbank:
            return None
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
        """現物と無期限先物の両方で上場している銘柄リストを返す。bitbank は現物+信用取引のみ。"""
        self._ensure_markets_loaded()
        if self.config.is_bitbank:
            return list(self.config.pair_symbols)

        perp = self._get_perpetual_exchange()
        if perp is None:
            return list(self.config.pair_symbols)

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
        """各銘柄の現在のファンディングレートを取得する。bitbank は先物なしのため空。"""
        if self.config.is_bitbank:
            return {s: 0.0 for s in symbols}
        perp = self._get_perpetual_exchange()
        if perp is None:
            return {s: 0.0 for s in symbols}
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
        if perp is None:
            return None
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
                    "percentage": float(ticker.get("percentage", 0)),
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

    def fetch_ohlcv_range(
        self,
        symbol: str,
        since_ms: int,
        end_ms: int,
        timeframe: str = "1h",
        min_candles: int = 200,
    ) -> list[list]:
        """指定期間のOHLCVを取得する。

        bitbank は Candlestick API が YYYYMMDD 指定で1日分しか返さないため、
        日単位でループして複数日分を取得する。Bybit/Binance はカーソルベースでループ。
        """
        self._ensure_markets_loaded()
        if self.config.is_bitbank:
            return self._fetch_ohlcv_range_bitbank(
                symbol, since_ms, end_ms, timeframe, min_candles
            )
        return self._fetch_ohlcv_range_generic(
            symbol, since_ms, end_ms, timeframe, min_candles
        )

    def _fetch_ohlcv_range_bitbank(
        self,
        symbol: str,
        since_ms: int,
        end_ms: int,
        timeframe: str,
        min_candles: int,
    ) -> list[list]:
        """bitbank: 日単位でAPIを呼び、指定期間のOHLCVを取得。"""
        one_day_ms = 24 * 3600 * 1000

        def _start_of_day_utc(ms: int) -> int:
            dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return int(start.timestamp() * 1000)

        result: list[list] = []
        current = _start_of_day_utc(since_ms)
        seen_ts: set[int] = set()

        while current < end_ms and len(result) < min_candles:
            candles = self.fetch_ohlcv(symbol, timeframe, since=current, limit=100)
            if not candles:
                current += one_day_ms
                time.sleep(0.2)
                continue
            for c in candles:
                ts = int(c[0])
                if ts < since_ms or ts > end_ms:
                    continue
                if ts not in seen_ts:
                    seen_ts.add(ts)
                    result.append(c)
            current += one_day_ms
            time.sleep(0.2)

        return sorted(result, key=lambda x: x[0])

    def _fetch_ohlcv_range_generic(
        self,
        symbol: str,
        since_ms: int,
        end_ms: int,
        timeframe: str,
        min_candles: int,
    ) -> list[list]:
        """Bybit/Binance 等: カーソルベースでループ取得。"""
        result: list[list] = []
        since = since_ms
        while since < end_ms and len(result) < min_candles:
            candles = self.fetch_ohlcv(symbol, timeframe, since=since, limit=200)
            if not candles:
                break
            for c in candles:
                if since_ms <= c[0] <= end_ms:
                    result.append(c)
            since = candles[-1][0] + 1
            time.sleep(0.2)
        return sorted(result, key=lambda x: x[0])

    def fetch_funding_rate_history(
        self,
        symbol: str,
        since: Optional[int] = None,
        limit: Optional[int] = 200,
    ) -> list[dict[str, Any]]:
        """過去のファンディングレート履歴を取得する。bitbank は先物なしのため空。"""
        if self.config.is_bitbank:
            return []
        perp = self._get_perpetual_exchange()
        if perp is None:
            return []
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

    def fetch_open_interest_history(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: Optional[int] = 30,
    ) -> list[dict[str, Any]]:
        """過去のOpen Interest（未決済建玉）履歴を取得する。bitbank は先物なしのため空。"""
        if self.config.is_bitbank:
            return []
        perp = self._get_perpetual_exchange()
        if perp is None:
            return []
        perp_symbol = self._to_perpetual_symbol(symbol)
        if perp_symbol is None:
            return []

        try:
            if hasattr(perp, "fetch_open_interest_history"):
                return perp.fetch_open_interest_history(
                    perp_symbol, timeframe=timeframe, since=since, limit=limit
                )
        except Exception:
            pass

        return []

    def get_oi_change_pct_24h(self, symbol: str) -> Optional[float]:
        """過去24時間のOI変化率（%）を取得する。取得失敗時はNone。bitbank は None。"""
        import time as _time

        if self.config.is_bitbank:
            return None
        perp = self._get_perpetual_exchange()
        if perp is None:
            return None
        perp_symbol = self._to_perpetual_symbol(symbol)
        if perp_symbol is None:
            return None

        try:
            if not hasattr(perp, "fetch_open_interest_history"):
                return None
            since = int(_time.time() * 1000) - 25 * 3600 * 1000
            oi_hist = self.fetch_open_interest_history(
                symbol, timeframe="1h", since=since, limit=30
            )
            if not oi_hist or len(oi_hist) < 2:
                return None
            oi_old = float(
                oi_hist[0].get("openInterestAmount", oi_hist[0].get("openInterest", 0))
            )
            oi_new = float(
                oi_hist[-1].get("openInterestAmount", oi_hist[-1].get("openInterest", 0))
            )
            if oi_old and oi_old > 0:
                return (oi_new - oi_old) / oi_old * 100
            return None
        except Exception:
            return None

    def get_oi_volume_ratio_pct(self, symbol: str) -> Optional[float]:
        """OI履歴が取れない場合のフォールバック: 現在OI（USD）と24h出来高の比率（%）を返す。"""
        if self.config.is_bitbank:
            return None
        perp = self._get_perpetual_exchange()
        if perp is None:
            return None
        perp_symbol = self._to_perpetual_symbol(symbol)
        if perp_symbol is None:
            return None
        try:
            oi = perp.fetch_open_interest(perp_symbol)
            oi_amt = float(oi.get("openInterestAmount", 0) or 0)
            tk = self.get_tickers([symbol])
            if symbol not in tk or not oi_amt:
                return None
            price = float(tk[symbol].get("last", 0) or 0)
            vol = float(tk[symbol].get("quoteVolume", 0) or 0)
            if vol and vol > 0 and price > 0:
                oi_usd = oi_amt * price
                return oi_usd / vol * 100
            return None
        except Exception:
            return None

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
            oi_change = self.get_oi_change_pct_24h(symbol)
            oi_vol_ratio = self.get_oi_volume_ratio_pct(symbol) if oi_change is None else None
            market_data.append(
                {
                    "symbol": symbol,
                    "funding_rate": fr,
                    "price": tk["last"],
                    "volume_24h": tk["volume"],
                    "quote_volume_24h": tk["quoteVolume"],
                    "price_change_pct_24h": tk.get("percentage", 0),
                    "oi_change_pct_24h": oi_change,
                    "oi_volume_ratio_pct": oi_vol_ratio,
                }
            )

        return {"symbols": symbols, "market_data": market_data}

    def fetch_crypto_news(self, limit: int = 10) -> list[str]:
        """ETH/BTC関連の最新ニュースヘッドラインを5〜10件取得する。

        CryptoPanic API（CRYPTOPANIC_API_KEY 設定時）を優先。
        未設定時はRSSフィード（CoinDesk, CoinTelegraph）から取得。

        Returns:
            ニュースタイトルのリスト（最大 limit 件）
        """
        if self.config.cryptopanic_api_key:
            return self._fetch_news_cryptopanic(limit)
        return self._fetch_news_rss(limit)

    def _fetch_news_cryptopanic(self, limit: int) -> list[str]:
        """CryptoPanic API からニュースを取得する。"""
        import json

        url = (
            f"{CRYPTOPANIC_API_URL}"
            f"?auth_token={self.config.cryptopanic_api_key}"
            f"&currencies=BTC,ETH"
            f"&filter=hot"
            f"&public=true"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AI-Crypto-Trader/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            results = data.get("results", [])
            titles = [r.get("title", "") for r in results[:limit] if r.get("title")]
            return titles
        except Exception as e:
            logger.warning("[News] CryptoPanic API 取得失敗: %s → RSS にフォールバック", e)
            return self._fetch_news_rss(limit)

    def _fetch_news_rss(self, limit: int) -> list[str]:
        """RSSフィードからETH/BTC関連ニュースを取得する。"""
        all_titles: list[str] = []
        for feed_url in RSS_FEEDS:
            try:
                req = urllib.request.Request(feed_url, headers={"User-Agent": "AI-Crypto-Trader/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    tree = ET.parse(resp)
                root = tree.getroot()
                ns = {"dc": "http://purl.org/dc/elements/1.1/"}
                for item in root.findall(".//item")[:limit]:
                    title_el = item.find("title")
                    if title_el is not None and title_el.text:
                        title = title_el.text.strip()
                        if NEWS_KEYWORDS.search(title):
                            all_titles.append(title)
            except Exception as e:
                logger.debug("[News] RSS %s 取得失敗: %s", feed_url[:40], e)
        return all_titles[:limit]
