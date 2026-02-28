"""銘柄選定モジュール。出来高・FRでフィルタリングし、FR上位銘柄を選定する（AIモック）。"""

from __future__ import annotations

from typing import Any

from config import Config


class Screener:
    """市場データをスクリーニングし、エントリー候補銘柄を選定するクラス。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.min_fr_threshold = config.min_fr_threshold
        self.min_volume_usd = 100_000

    def screen(self, market_data: dict[str, Any]) -> list[dict[str, Any]]:
        """出来高過少・FRマイナス銘柄を除外し、候補リストを返す。"""
        data_list = market_data.get("market_data", [])
        candidates: list[dict[str, Any]] = []

        for item in data_list:
            fr = item.get("funding_rate", 0)
            quote_vol = item.get("quote_volume_24h", 0)

            if fr < self.min_fr_threshold:
                continue
            if quote_vol < self.min_volume_usd:
                continue

            candidates.append(item)

        return candidates

    def select_top(
        self, candidates: list[dict[str, Any]], top_n: int | None = None
    ) -> list[dict[str, Any]]:
        """FR上位N銘柄を選定する（AIモック関数）。将来的にAI推論に差し替え可能。"""
        n = top_n or self.config.max_positions
        sorted_candidates = sorted(
            candidates, key=lambda x: x.get("funding_rate", 0), reverse=True
        )
        return sorted_candidates[:n]

    def run(self, market_data: dict[str, Any]) -> list[dict[str, Any]]:
        """スクリーニングと選定を一括実行する。"""
        candidates = self.screen(market_data)
        return self.select_top(candidates)
