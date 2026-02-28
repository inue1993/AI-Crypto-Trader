#!/usr/bin/env python3
"""運用 CLI ツール。デプロイ済みシステムの状態確認・操作を行う。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# boto3 は Lambda 用に requirements に含める。ops はローカル実行用
try:
    import boto3
except ImportError:
    print("ops.py には boto3 が必要です: pip install boto3", file=sys.stderr)
    sys.exit(1)


def get_table_name() -> str:
    """DynamoDB テーブル名を取得する。"""
    name = os.getenv("DYNAMODB_TABLE")
    if not name:
        print("DYNAMODB_TABLE が未設定です。.env または環境変数を設定してください。", file=sys.stderr)
        sys.exit(1)
    return name


def get_function_name() -> str:
    """Lambda 関数名を取得する。"""
    name = os.getenv("LAMBDA_FUNCTION_NAME")
    if not name:
        # デフォルト: samconfig の stack_name に合わせる
        stack = os.getenv("SAM_STACK_NAME", "ai-crypto-trader")
        name = f"{stack}-CryptoTrader"
    return name


def get_stack_name() -> str:
    """CloudFormation スタック名を取得する。"""
    return os.getenv("SAM_STACK_NAME", "ai-crypto-trader")


def _get_lambda_mode() -> str:
    """Lambda の MODE 環境変数を取得する。失敗時は 'N/A'。"""
    try:
        client = boto3.client("lambda")
        resp = client.get_function_configuration(FunctionName=get_function_name())
        return resp.get("Environment", {}).get("Variables", {}).get("MODE", "N/A")
    except Exception:
        return "N/A"


def _get_eventbridge_info() -> tuple[str, str]:
    """EventBridge ルールのスケジュールと状態を取得。失敗時は ('N/A', 'N/A')。"""
    try:
        client = boto3.client("events")
        rule_name = f"{get_stack_name()}-Hourly"
        resp = client.describe_rule(Name=rule_name)
        return (
            resp.get("ScheduleExpression", "N/A"),
            resp.get("State", "N/A"),
        )
    except Exception:
        return ("N/A", "N/A")


def _get_stack_deployed_at() -> str:
    """CloudFormation スタックの初回デプロイ日時を取得。失敗時は 'N/A'。"""
    try:
        client = boto3.client("cloudformation")
        resp = client.describe_stacks(StackName=get_stack_name())
        stacks = resp.get("Stacks", [])
        if not stacks:
            return "N/A"
        created = stacks[0].get("CreationTime")
        if created:
            return created.strftime("%Y-%m-%d %H:%M:%S UTC")
        return "N/A"
    except Exception:
        return "N/A"


def cmd_status(args: argparse.Namespace) -> int:
    """現在のポジション状態と直近の Z-Score を表示。"""
    from storage import DynamoDBStorage

    table = get_table_name()
    store = DynamoDBStorage(table)

    state = store.load_position_state()
    latest = store.get_latest_monitor()

    mode = _get_lambda_mode()
    schedule, rule_state = _get_eventbridge_info()
    deployed_at = _get_stack_deployed_at()

    print("=== Lambda 設定 ===")
    print(f"  mode: {mode}")

    print("\n=== EventBridge ===")
    print(f"  deployed_at: {deployed_at}")
    print(f"  schedule: {schedule}")
    print(f"  state: {rule_state}")

    print("\n=== ポジション状態 ===")
    if state:
        print(f"  status: {state.get('status', 'N/A')}")
        if state.get("status") == "OPEN":
            print(f"  direction: {state.get('direction')}")
            print(f"  entry_z_score: {state.get('entry_z_score')}")
            print(f"  eth_entry: {state.get('eth_entry_price')} | btc_entry: {state.get('btc_entry_price')}")
            print(f"  position_size_usd: {state.get('position_size_usd')}")
    else:
        print("  status: NO_POSITION (未初期化)")

    print("\n=== 直近モニタリング ===")
    if latest:
        print(f"  timestamp: {latest.get('sk')}")
        print(f"  Z-Score: {latest.get('z_score')}")
        print(f"  Ratio: {latest.get('ratio')}")
        print(f"  ETH: {latest.get('eth_price')} | BTC: {latest.get('btc_price')}")
    else:
        print("  データなし")

    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """過去 N 日間のモニタリング履歴を表示。"""
    from storage import DynamoDBStorage

    table = get_table_name()
    store = DynamoDBStorage(table)
    days = args.days or 7

    monitors = store.query_recent_monitors(days=days)
    if not monitors:
        print(f"過去 {days} 日間のデータはありません。")
        return 0

    try:
        import pandas as pd

        df = pd.DataFrame(monitors)
        df = df.rename(columns={"sk": "timestamp"})
        cols = ["timestamp", "z_score", "ratio", "eth_price", "btc_price", "position_status"]
        cols = [c for c in cols if c in df.columns]
        df = df[cols].head(args.limit or 50)
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", None)
        print(df.to_string(index=False))
    except ImportError:
        for m in monitors[: args.limit or 50]:
            print(m)

    return 0


def cmd_trades(args: argparse.Namespace) -> int:
    """トレード履歴を表示。"""
    from storage import DynamoDBStorage

    table = get_table_name()
    store = DynamoDBStorage(table)

    limit = None if getattr(args, "all_", False) else (args.limit or 50)
    trades = store.query_trades(limit=limit)
    if not trades:
        print("トレード履歴はありません。")
        return 0

    total_pnl = sum(float(t.get("pnl_usd") or 0) for t in trades)
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0) > 0)

    print("=== トレード履歴 ===")
    for t in trades:
        ts = t.get("sk", "")
        direction = t.get("direction", "")
        pnl = t.get("pnl_usd", 0)
        reason = t.get("exit_reason", "")
        print(f"  {ts} | {direction} | PnL: ${pnl:.2f} | {reason}")

    print(f"\n合計: {len(trades)} 件 | 勝ち: {wins} | 合計 PnL: ${total_pnl:.2f}")
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    """Lambda を手動で即時実行。"""
    func_name = get_function_name()
    client = boto3.client("lambda")

    try:
        resp = client.invoke(
            FunctionName=func_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({}),
        )
        payload = json.loads(resp["Payload"].read())
        print("Lambda 実行結果:")
        print(json.dumps(payload, indent=2, default=str))
        if resp.get("FunctionError"):
            print(f"エラー: {resp['FunctionError']}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"Lambda 呼び出し失敗: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """ポジション状態を NO_POSITION にリセット（緊急停止）。"""
    from storage import DynamoDBStorage

    table = get_table_name()
    store = DynamoDBStorage(table)

    state = store.load_position_state()
    if state and state.get("status") == "OPEN":
        print("警告: オープンポジションがあります。")
        print("この操作は DynamoDB の状態のみをリセットします。")
        print("取引所の実ポジションは閉じません。手動で決済してください。")
        if not args.yes:
            confirm = input("続行しますか? [y/N]: ")
            if confirm.lower() != "y":
                print("キャンセルしました。")
                return 0

    ok = store.reset_position_state()
    if ok:
        print("ポジション状態を NO_POSITION にリセットしました。")
    else:
        print("リセットに失敗しました。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-Crypto-Trader 運用 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="現在のポジション状態と直近 Z-Score")
    p_status.set_defaults(func=cmd_status)

    # logs
    p_logs = subparsers.add_parser("logs", help="過去 N 日間のモニタリング履歴")
    p_logs.add_argument("--days", type=int, default=7)
    p_logs.add_argument("--limit", type=int, default=50)
    p_logs.set_defaults(func=cmd_logs)

    # trades
    p_trades = subparsers.add_parser("trades", help="トレード履歴")
    p_trades.add_argument("--limit", type=int, default=50, help="表示件数（デフォルト: 50）")
    p_trades.add_argument("--all", dest="all_", action="store_true", help="全件表示")
    p_trades.set_defaults(func=cmd_trades)

    # invoke
    p_invoke = subparsers.add_parser("invoke", help="Lambda を手動で即時実行")
    p_invoke.set_defaults(func=cmd_invoke)

    # stop
    p_stop = subparsers.add_parser("stop", help="ポジション状態をリセット（緊急停止）")
    p_stop.add_argument("-y", "--yes", action="store_true", help="確認なしで実行")
    p_stop.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
