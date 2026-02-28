"""設定管理モジュール。環境変数から設定を読み込み、Config dataclass に格納する。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from dotenv import load_dotenv
import os


class Mode(str, Enum):
    """動作モード。"""

    LIVE = "LIVE"
    DRY_RUN = "DRY_RUN"
    BACKTEST = "BACKTEST"


@dataclass
class Config:
    """アプリケーション設定。"""

    mode: Mode
    exchange: str
    api_key: str
    api_secret: str
    initial_capital: float = 10000.0
    max_positions: int = 3
    min_fr_threshold: float = 0.0001
    taker_fee: float = 0.001
    slippage: float = 0.0005
    backtest_start: Optional[str] = None
    backtest_end: Optional[str] = None
    backtest_symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])

    @classmethod
    def from_env(cls) -> "Config":
        """環境変数から設定を読み込む。"""
        load_dotenv()

        mode_str = os.getenv("MODE", "DRY_RUN").upper()
        try:
            mode = Mode(mode_str)
        except ValueError:
            mode = Mode.DRY_RUN

        backtest_symbols_str = os.getenv("BACKTEST_SYMBOLS", "BTC/USDT,ETH/USDT")
        backtest_symbols = [s.strip() for s in backtest_symbols_str.split(",") if s.strip()]

        return cls(
            mode=mode,
            exchange=os.getenv("EXCHANGE", "bybit").lower(),
            api_key=os.getenv("API_KEY", ""),
            api_secret=os.getenv("API_SECRET", ""),
            initial_capital=float(os.getenv("INITIAL_CAPITAL", "10000")),
            max_positions=int(os.getenv("MAX_POSITIONS", "3")),
            min_fr_threshold=float(os.getenv("MIN_FR_THRESHOLD", "0.000005")),
            taker_fee=float(os.getenv("TAKER_FEE", "0.001")),
            slippage=float(os.getenv("SLIPPAGE", "0.0005")),
            backtest_start=os.getenv("BACKTEST_START", "2024-09-24"),
            backtest_end=os.getenv("BACKTEST_END", "2024-10-31"),
            backtest_symbols=backtest_symbols,
        )
