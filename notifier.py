"""Slack Webhook 通知モジュール。"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Slack Incoming Webhook で通知を送信する。"""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url or ""

    def _send(self, payload: dict[str, Any]) -> bool:
        """Slack にペイロードを送信する。"""
        if not self.webhook_url:
            logger.debug("SLACK_WEBHOOK_URL 未設定のため通知をスキップ")
            return False
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 204):
                    return True
                logger.warning("Slack 通知失敗: status=%d", resp.status)
                return False
        except Exception as e:
            logger.warning("Slack 通知エラー: %s", e)
            return False

    def send_signal_alert(self, result: dict[str, Any]) -> bool:
        """シグナル検出イベントを通知する。"""
        z = result.get("z_score", 0)
        decision = result.get("ai_decision", "PASS")
        confidence = result.get("ai_confidence", 0)
        reason = result.get("ai_reason", "") or "(なし)"
        eth_price = result.get("eth_price", 0)
        btc_price = result.get("btc_price", 0)

        text = (
            f"*シグナル検出* | Z-Score={z:.2f}\n"
            f"ETH/BTC: {eth_price:.2f} / {btc_price:.2f}\n"
            f"AI判定: {decision} (confidence={confidence})\n"
            f"理由: {reason[:200]}"
        )
        payload = {
            "text": text,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "🔔 ペアトレード シグナル"}, "emoji": True},
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ],
        }
        return self._send(payload)

    def send_entry_alert(self, result: dict[str, Any], state: dict[str, Any]) -> bool:
        """エントリー実行を通知する（LIVE）。"""
        direction = state.get("direction", "")
        z = result.get("z_score", 0)
        eth_price = result.get("eth_price", 0)
        btc_price = result.get("btc_price", 0)
        size_usd = state.get("position_size_usd", 0)

        text = (
            f"*エントリー実行* | {direction}\n"
            f"Z-Score={z:.2f} | ETH={eth_price:.2f} BTC={btc_price:.2f}\n"
            f"ポジションサイズ: ${size_usd:.2f}"
        )
        payload = {
            "text": text,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "📈 ペアトレード エントリー"}, "emoji": True},
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ],
        }
        return self._send(payload)

    def send_exit_alert(self, result: dict[str, Any], trade: dict[str, Any]) -> bool:
        """エグジット実行を通知する（LIVE）。"""
        direction = trade.get("direction", "")
        exit_reason = trade.get("exit_reason", "")
        pnl_usd = trade.get("pnl_usd", 0)
        pnl_pct = trade.get("pnl_pct", 0)
        entry_z = trade.get("entry_z") or 0
        exit_z = trade.get("exit_z") or 0

        emoji = "✅" if pnl_usd >= 0 else "❌"
        text = (
            f"*{emoji} 決済完了* | {direction}\n"
            f"理由: {exit_reason}\n"
            f"PnL: ${pnl_usd:.2f} ({pnl_pct:+.2f}%)\n"
            f"Z-Score: {entry_z:.2f} → {exit_z:.2f}"
        )
        payload = {
            "text": text,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "📉 ペアトレード 決済"}, "emoji": True},
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ],
        }
        return self._send(payload)
