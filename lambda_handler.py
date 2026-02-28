"""Lambda エントリーポイント。EventBridge から1時間ごとに起動される。"""

from __future__ import annotations

import json
import logging
import os

from config import Config
from main import run_once
from notifier import SlackNotifier
from storage import DynamoDBStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def handler(event: dict, context: object) -> dict:
    """Lambda ハンドラー。1回分の監視・取引ロジックを実行する。"""
    config = Config.from_env()

    if not config.dynamodb_table:
        logger.error("DYNAMODB_TABLE が未設定です。")
        return {"statusCode": 500, "body": json.dumps({"error": "DYNAMODB_TABLE required"})}

    storage = DynamoDBStorage(config.dynamodb_table, ttl_days=config.log_ttl_days)
    notifier = SlackNotifier(config.slack_webhook_url) if config.slack_webhook_url else None

    try:
        result = run_once(config, storage=storage, notifier=notifier)
    except Exception as e:
        logger.exception("run_once エラー: %s", e)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}, default=str),
        }

    if result is None or result.get("_fetch_failed"):
        detail = result.get("detail", "") if result else ""
        reason = result.get("reason", "data_fetch_failed") if result else "data_fetch_failed"
        logger.warning("データ取得失敗のためスキップ: %s %s", reason, detail)
        return {
            "statusCode": 200,
            "body": json.dumps({"skipped": True, "reason": reason, "detail": detail}),
        }

    # モニタリング結果を保存
    storage.save_monitor_log(result)

    # シグナル検出時（AI判定前）は保存・通知は run_once 内で実施
    # trade_executed 時も run_once 内で通知済み
    # ここでは signal_triggered かつ AI が ENTRY でなかった場合のシグナル保存
    if result.get("signal_triggered") and not result.get("trade_executed"):
        storage.save_signal(result)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "z_score": result.get("z_score"),
                "position_status": result.get("position_status"),
                "signal_triggered": result.get("signal_triggered"),
                "trade_executed": result.get("trade_executed"),
            },
            default=str,
        ),
    }
