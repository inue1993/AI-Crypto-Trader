#!/usr/bin/env python3
"""DynamoDB テーブルを自動作成するスクリプト。SAM デプロイ前にローカル開発用にテーブルを作成する場合に使用。"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import boto3
except ImportError:
    print("boto3 が必要です: pip install boto3", file=sys.stderr)
    sys.exit(1)


def create_table(table_name: str | None = None, region: str | None = None) -> str:
    """DynamoDB テーブルを作成する。既存の場合はスキップ。"""
    table_name = table_name or os.getenv("DYNAMODB_TABLE", "ai-crypto-trader-CryptoTrader")
    region = region or os.getenv("AWS_REGION", "ap-northeast-1")

    client = boto3.client("dynamodb", region_name=region)

    try:
        client.describe_table(TableName=table_name)
        print(f"テーブル {table_name} は既に存在します。")
        return table_name
    except client.exceptions.ResourceNotFoundException:
        pass

    client.create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        TimeToLiveSpecification={
            "AttributeName": "ttl",
            "Enabled": True,
        },
    )

    print(f"テーブル {table_name} を作成しました。")
    print("TTL の有効化には数分かかる場合があります。")
    return table_name


if __name__ == "__main__":
    name = create_table()
    print(f"\n.env に以下を設定してください:")
    print(f"  DYNAMODB_TABLE={name}")
