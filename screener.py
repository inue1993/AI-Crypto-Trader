"""ペアトレード用スクリーナー。Z-Score計算とAIファンダメンタルズ・フィルター。"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
import pandas as pd

from config import Config

if TYPE_CHECKING:
    from fetcher import DataFetcher

logger = logging.getLogger(__name__)

# 取引コスト（手数料＋スプレッド）: 0.15% per side
TRANSACTION_COST_RATE = 0.0015

# Z-Score 閾値
Z_SCORE_ENTRY_THRESHOLD = 2.0
Z_SCORE_EXIT_THRESHOLD = 0.5  # 平均回帰: |Z| <= 0.5 でエグジット（厳密な0は稀なため）
Z_SCORE_STOP_LOSS = 3.5  # ハード・ストップロス: |Z| > 3.5 で即座に損切り

# 移動平均・標準偏差のウィンドウ（1時間足の過去期間数）
ROLLING_WINDOW = 200

# AI判定
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
AI_API_SLEEP_SEC = 1.0  # レートリミット回避
AI_TIMEOUT_SEC = 30
MIN_CONFIDENCE_FOR_ENTRY = 70  # confidence > 70 でエントリー許可

# ペアトレード用システムプロンプト（バックテスト用：価格データのみ）
PAIR_TRADE_SYSTEM_PROMPT = """あなたは暗号通貨のクオンツファンドマネージャーです。
現在、ETHとBTCの価格比率に統計的な異常値（2シグマ以上の乖離）が発生しています。

【重要】直前24時間の騰落率を参考に、この乖離が『ETHのハッキングや大型アップデートの失敗など、根本的なファンダメンタルズの変化』によるものか、
それとも『一時的な需給の歪み（ノイズ）』かを判定してください。

もし根本的な理由がない（ノイズである）と推測される場合は "ENTRY"（勝率が高い）とし、
致命的なニュースがある場合は "PASS"（危険）としてください。

必ず以下のJSON形式のみで回答してください。他のテキストは含めないでください。
{"decision": "ENTRY" | "PASS", "confidence": 0-100の整数, "reason": "判定理由を簡潔に"}"""

# DRY_RUN用：ニュースベースの判定（本物のテキストデータを注入）
PAIR_TRADE_SYSTEM_PROMPT_WITH_NEWS = """あなたは暗号通貨クオンツファンドのマネージャーです。
現在、Zスコアの異常値（価格乖離）が発生しています。

以下の【最新ニュース一覧】を読み、この乖離が「ハッキングや規制強化などの重大な悪材料」によるものか判定してください。

ニュースに悪材料が含まれている場合は絶対に "PASS" とし、悪材料が見当たらないノイズ下落の場合のみ "ENTRY" を出力してください。

必ず以下のJSON形式のみで回答してください。他のテキストは含めないでください。
{"decision": "ENTRY" | "PASS", "confidence": 0-100の整数, "reason": "判定理由を簡潔に（PASSの場合は悪材料のニュースを記載）"}"""


def calc_z_score(
    eth_closes: pd.Series,
    btc_closes: pd.Series,
    window: int = ROLLING_WINDOW,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Ratio, Rolling Mean, Rolling Std, Z-Score を計算する。

    Args:
        eth_closes: ETH の終値シリーズ
        btc_closes: BTC の終値シリーズ
        window: 移動平均・標準偏差のウィンドウサイズ

    Returns:
        (ratio, rolling_mean, rolling_std, z_score)
    """
    ratio = eth_closes / btc_closes
    rolling_mean = ratio.rolling(window=window, min_periods=window).mean()
    rolling_std = ratio.rolling(window=window, min_periods=window).std()
    z_score = (ratio - rolling_mean) / rolling_std
    z_score = z_score.replace([np.inf, -np.inf], np.nan)
    return ratio, rolling_mean, rolling_std, z_score


def ai_decision_mock(
    z_score: float,
    ratio: float,
    eth_price: float,
    btc_price: float,
    eth_change_24h_pct: Optional[float] = None,
    btc_change_24h_pct: Optional[float] = None,
) -> dict[str, Any]:
    """バックテスト用: 常に ENTRY を返すダミー関数。"""
    return {
        "decision": "ENTRY",
        "confidence": 100,
        "reason": "Backtest mock: always ENTRY",
    }


class PairTradeScreener:
    """ペアトレード用スクリーナー。Z-Score 計算と AI 判定。"""

    def __init__(self, config: Config, fetcher: Optional["DataFetcher"] = None) -> None:
        self.config = config
        self.fetcher = fetcher
        self._client = None

    def _get_openai_client(self):
        """OpenAI互換クライアント（DeepSeek向け）を取得する。"""
        if self._client is None and self.config.deepseek_api_key:
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=self.config.deepseek_api_key,
                    base_url=DEEPSEEK_BASE_URL,
                )
            except ImportError:
                logger.warning("openai がインストールされていません。pip install openai を実行してください。")
        return self._client

    def check_entry_signal(self, z_score: float) -> tuple[bool, Optional[str]]:
        """Z-Score がエントリー閾値を超えているか判定する。

        ストップロス域（|Z| > 3.5）ではエントリーしない。
        異常値の真っ只中で入るのは危険なため。

        Returns:
            (should_enter, direction)
            direction: "long_eth_short_btc" | "short_eth_long_btc" | None
        """
        if pd.isna(z_score):
            return False, None
        # ストップロス域ではエントリー禁止（入った瞬間に損切りになる）
        if abs(z_score) > Z_SCORE_STOP_LOSS:
            return False, None
        if z_score < -Z_SCORE_ENTRY_THRESHOLD:
            return True, "long_eth_short_btc"
        if z_score > Z_SCORE_ENTRY_THRESHOLD:
            return True, "short_eth_long_btc"
        return False, None

    def check_exit_signal(self, z_score: float) -> bool:
        """Z-Score が平均（0）に戻ったか判定する。"""
        if pd.isna(z_score):
            return False
        return abs(z_score) <= abs(Z_SCORE_EXIT_THRESHOLD) + 1e-9

    def check_stop_loss(self, z_score: float, direction: str) -> bool:
        """ハード・ストップロス: Z-Score が異常値（±3.5超）に達したら即座に損切り。

        Args:
            z_score: 現在の Z-Score
            direction: "long_eth_short_btc" | "short_eth_long_btc"

        Returns:
            True なら損切り実行
        """
        if pd.isna(z_score):
            return False
        if direction == "long_eth_short_btc":
            # ETHロング・BTCショート: Z がさらにマイナス（ETHがさらに売られる）→ 含み損拡大
            return z_score < -Z_SCORE_STOP_LOSS
        if direction == "short_eth_long_btc":
            # ETHショート・BTCロング: Z がさらにプラス（ETHがさらに買われる）→ 含み損拡大
            return z_score > Z_SCORE_STOP_LOSS
        return False

    def ai_decision(
        self,
        z_score: float,
        ratio: float,
        eth_price: float,
        btc_price: float,
        use_mock: bool = True,
        eth_change_24h_pct: Optional[float] = None,
        btc_change_24h_pct: Optional[float] = None,
        news_titles: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """AI によるファンダメンタルズ・フィルター判定。

        Args:
            z_score: 現在の Z-Score
            ratio: 現在の ETH/BTC 比率
            eth_price: ETH 価格
            btc_price: BTC 価格
            use_mock: True の場合は常に ENTRY を返す（バックテスト用）
            eth_change_24h_pct: 直前24時間のETH騰落率（%）
            btc_change_24h_pct: 直前24時間のBTC騰落率（%）
            news_titles: 最新ニュースタイトルリスト（DRY_RUN時、本物のテキストを注入）

        Returns:
            {"decision": "ENTRY"|"PASS", "confidence": 0-100, "reason": "..."}
        """
        if use_mock:
            return ai_decision_mock(
                z_score, ratio, eth_price, btc_price,
                eth_change_24h_pct=eth_change_24h_pct,
                btc_change_24h_pct=btc_change_24h_pct,
            )

        client = self._get_openai_client()
        if not client:
            return {"decision": "PASS", "confidence": 0, "reason": "API未設定"}

        # ニュースあり: ニュースベースのプロンプトを使用
        if news_titles:
            system_prompt = PAIR_TRADE_SYSTEM_PROMPT_WITH_NEWS
            news_text = "\n".join(f"- {t}" for t in news_titles[:10])
            eth_chg = eth_change_24h_pct if eth_change_24h_pct is not None else 0.0
            btc_chg = btc_change_24h_pct if btc_change_24h_pct is not None else 0.0
            user_content = (
                f"【Zスコア】{z_score:.2f} | "
                f"24h騰落率: BTC {btc_chg:+.2f}%, ETH {eth_chg:+.2f}%\n\n"
                f"【最新ニュース一覧】\n{news_text}"
            )
        else:
            # ニュースなし（バックテスト互換）: 価格データのみのプロンプト
            system_prompt = PAIR_TRADE_SYSTEM_PROMPT
            eth_chg = eth_change_24h_pct if eth_change_24h_pct is not None else 0.0
            btc_chg = btc_change_24h_pct if btc_change_24h_pct is not None else 0.0
            if z_score < 0:
                situation = (
                    f"現在ETHがBTCに対して異常に売られています（Z-Score {z_score:.2f}）。"
                    f"直前24時間でBTCは{btc_chg:+.2f}%、ETHは{eth_chg:+.2f}%です。"
                )
            else:
                situation = (
                    f"現在ETHがBTCに対して異常に買われています（Z-Score {z_score:.2f}）。"
                    f"直前24時間でBTCは{btc_chg:+.2f}%、ETHは{eth_chg:+.2f}%です。"
                )
            user_content = json.dumps(
                {
                    "situation": situation,
                    "z_score": z_score,
                    "ratio": ratio,
                    "eth_price_usd": eth_price,
                    "btc_price_usd": btc_price,
                    "eth_change_24h_pct": eth_chg,
                    "btc_change_24h_pct": btc_chg,
                },
                ensure_ascii=False,
            )

        time.sleep(AI_API_SLEEP_SEC)
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
                timeout=AI_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.warning("[AI] ペアトレード判定 APIエラー: %s → PASS（安全側）", e)
            time.sleep(AI_API_SLEEP_SEC)
            return {"decision": "PASS", "confidence": 0, "reason": f"APIエラー: {e}"}

        time.sleep(AI_API_SLEEP_SEC)
        raw = response.choices[0].message.content
        try:
            parsed = json.loads(raw)
            decision = parsed.get("decision", "PASS")
            confidence = int(parsed.get("confidence", 0))
            reason = str(parsed.get("reason", ""))
            if decision not in ("ENTRY", "PASS"):
                decision = "PASS"
            return {
                "decision": decision,
                "confidence": max(0, min(100, confidence)),
                "reason": reason,
            }
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("[AI] JSONパース失敗: %s → PASS（安全側）", e)
            time.sleep(AI_API_SLEEP_SEC)
            return {"decision": "PASS", "confidence": 0, "reason": f"パースエラー: {e}"}
