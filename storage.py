"""DynamoDB 永続化レイヤー。ポジション状態・モニタリングログ・トレード履歴を保存する。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import boto3


def _ttl_epoch(days: int) -> int:
    """TTL 用の Unix タイムスタンプ（days 日後）を返す。"""
    return int(time.time()) + days * 86400


def _iso_now() -> str:
    """現在時刻を ISO 8601 形式で返す。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_decimal(val: Any) -> Any:
    """DynamoDB 用に float を Decimal に変換。"""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return Decimal(str(val))
    return val


class DynamoDBStorage:
    """DynamoDB への読み書きを行うストレージクラス。"""

    def __init__(self, table_name: str, ttl_days: int = 90) -> None:
        self.table_name = table_name
        self.ttl_days = ttl_days
        self._resource = boto3.resource("dynamodb")
        self._table = self._resource.Table(table_name)

    def load_position_state(self) -> Optional[dict[str, Any]]:
        """現在のポジション状態を取得する。"""
        try:
            resp = self._table.get_item(Key={"pk": "STATE", "sk": "position"})
            item = resp.get("Item")
            if not item:
                return None
            return self._from_ddb(item)
        except Exception:
            return None

    def save_position_state(self, state: dict[str, Any]) -> None:
        """ポジション状態を保存する。"""
        item = {
            "pk": "STATE",
            "sk": "position",
            "status": state.get("status", "NO_POSITION"),
            "updated_at": _iso_now(),
        }
        if state.get("status") == "OPEN":
            item["direction"] = state.get("direction", "")
            item["entry_z_score"] = _to_decimal(state.get("entry_z_score"))
            item["entry_ratio"] = _to_decimal(state.get("entry_ratio"))
            item["eth_entry_price"] = _to_decimal(state.get("eth_entry_price"))
            item["btc_entry_price"] = _to_decimal(state.get("btc_entry_price"))
            item["position_size_usd"] = _to_decimal(state.get("position_size_usd"))
            item["entry_timestamp"] = _to_decimal(state.get("entry_timestamp"))

        self._table.put_item(Item=item)

    def save_monitor_log(self, result: dict[str, Any]) -> None:
        """毎時のモニタリング結果を保存する。"""
        ts = _iso_now()
        item = {
            "pk": "MONITOR",
            "sk": ts,
            "z_score": _to_decimal(result.get("z_score")),
            "ratio": _to_decimal(result.get("ratio")),
            "eth_price": _to_decimal(result.get("eth_price")),
            "btc_price": _to_decimal(result.get("btc_price")),
            "eth_change_24h": _to_decimal(result.get("eth_change_24h")) if result.get("eth_change_24h") is not None else None,
            "btc_change_24h": _to_decimal(result.get("btc_change_24h")) if result.get("btc_change_24h") is not None else None,
            "position_status": result.get("position_status", "NO_POSITION"),
            "ttl": _ttl_epoch(self.ttl_days),
        }
        item = {k: v for k, v in item.items() if v is not None}
        self._table.put_item(Item=item)

    def save_signal(self, result: dict[str, Any]) -> None:
        """シグナル検出イベントを保存する。"""
        ts = _iso_now()
        item = {
            "pk": "SIGNAL",
            "sk": ts,
            "z_score": _to_decimal(result.get("z_score")),
            "ai_decision": result.get("ai_decision"),
            "ai_confidence": int(result.get("ai_confidence") or 0),
            "ai_reason": (result.get("ai_reason") or "")[:500],
            "news_count": len(result.get("news_titles", [])),
            "ttl": _ttl_epoch(self.ttl_days),
        }
        self._table.put_item(Item=item)

    def save_trade(self, trade: dict[str, Any]) -> None:
        """完了したトレードを保存する。"""
        ts = _iso_now()
        item = {
            "pk": "TRADE",
            "sk": ts,
            "direction": trade.get("direction"),
            "entry_z": _to_decimal(trade.get("entry_z")),
            "exit_z": _to_decimal(trade.get("exit_z")),
            "eth_entry_price": _to_decimal(trade.get("eth_entry_price")),
            "btc_entry_price": _to_decimal(trade.get("btc_entry_price")),
            "eth_exit_price": _to_decimal(trade.get("eth_exit_price")),
            "btc_exit_price": _to_decimal(trade.get("btc_exit_price")),
            "pnl_usd": _to_decimal(trade.get("pnl_usd")),
            "pnl_pct": _to_decimal(trade.get("pnl_pct")),
            "exit_reason": trade.get("exit_reason"),
            "duration_hours": _to_decimal(trade.get("duration_hours")),
            "ttl": _ttl_epoch(self.ttl_days),
        }
        self._table.put_item(Item=item)

    def query_recent_monitors(self, days: int = 7) -> list[dict[str, Any]]:
        """過去 N 日間のモニタリングログを取得する。"""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            resp = self._table.query(
                KeyConditionExpression="pk = :pk AND sk >= :since",
                ExpressionAttributeValues={":pk": "MONITOR", ":since": since},
                ScanIndexForward=False,
                Limit=500,
            )
            return [self._from_ddb(i) for i in resp.get("Items", [])]
        except Exception:
            return []

    def query_trades(self, limit: Optional[int] = 100) -> list[dict[str, Any]]:
        """トレード履歴を取得する。limit=None で全件取得（ページネーション）。"""
        try:
            items: list[dict[str, Any]] = []
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": "pk = :pk",
                "ExpressionAttributeValues": {":pk": "TRADE"},
                "ScanIndexForward": False,
            }
            if limit is not None:
                kwargs["Limit"] = limit

            while True:
                resp = self._table.query(**kwargs)
                items.extend(resp.get("Items", []))
                if limit is not None or "LastEvaluatedKey" not in resp:
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            return [self._from_ddb(i) for i in items]
        except Exception:
            return []

    def get_latest_monitor(self) -> Optional[dict[str, Any]]:
        """直近のモニタリング結果を1件取得する。"""
        try:
            resp = self._table.query(
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": "MONITOR"},
                ScanIndexForward=False,
                Limit=1,
            )
            items = resp.get("Items", [])
            return self._from_ddb(items[0]) if items else None
        except Exception:
            return None

    def reset_position_state(self) -> bool:
        """ポジション状態を NO_POSITION にリセットする。"""
        try:
            self.save_position_state({"status": "NO_POSITION"})
            return True
        except Exception:
            return False

    @staticmethod
    def _from_ddb(item: dict[str, Any]) -> dict[str, Any]:
        """DynamoDB の Decimal 等を Python 型に変換する。"""
        result = {}
        for k, v in item.items():
            if isinstance(v, Decimal):
                result[k] = float(v)
            else:
                result[k] = v
        return result
