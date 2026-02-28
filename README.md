# AI Crypto Trader

暗号通貨のデルタニュートラル・ファンディングレート（FR）アービトラージボット。無期限先物のFR歪みを利用し、現物買いと先物売りを同数量発注することで価格変動リスクを排除しつつFR収益を獲得する自動売買システム。

## 特徴

- **デルタニュートラル戦略**: 現物と先物を同数量で保有し、価格変動リスクをヘッジ
- **コスト考慮エントリー**: 想定FR収益が往復手数料を回収可能な銘柄のみエントリー
- **ホールド重視**: 最低保持期間（20日）+ 持続的マイナスFR確認後のみ退出
- **3つの動作モード**: LIVE（実運用）、DRY_RUN（仮想運用）、BACKTEST（過去検証）
- **対応取引所**: Binance / Bybit（ccxt 経由）

## 必要条件

- Python 3.10 以上

## セットアップ

```bash
git clone <repository-url>
cd AI-Crypto-Trader

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# .env を編集して API_KEY, API_SECRET 等を設定
```

## 設定

`.env` で以下を設定する。

| 変数 | 説明 | デフォルト |
|------|------|------------|
| `MODE` | 動作モード: LIVE / DRY_RUN / BACKTEST | DRY_RUN |
| `EXCHANGE` | 取引所: binance / bybit | bybit |
| `API_KEY` | API キー（LIVE 時必須） | - |
| `API_SECRET` | API シークレット（LIVE 時必須） | - |
| `DEEPSEEK_API_KEY` | DeepSeek API キー（AI銘柄選定、DRY_RUN/LIVE時） | - |
| `INITIAL_CAPITAL` | 初期資金（USD） | 10000 |
| `MAX_POSITIONS` | 最大同時ポジション数 | 2 |
| `TAKER_FEE` | 取引手数料率 | 0.001 |
| `SLIPPAGE` | 推定スリッページ率 | 0.0005 |
| `BACKTEST_START` | バックテスト開始日 | 2025-11-01 |
| `BACKTEST_END` | バックテスト終了日 | 2026-02-28 |
| `BACKTEST_SYMBOLS` | バックテスト対象銘柄（カンマ区切り） | BTC/USDT,ETH/USDT |

## 使い方

### バックテスト

```bash
MODE=BACKTEST python main.py
```

- 指定期間の OHLCV と FR 履歴を API から取得してシミュレーション
- `DEEPSEEK_API_KEY` 設定時、AI銘柄選定が有効（各候補でDeepSeek APIを呼び出し。APIコストに注意）
- 結果は `equity_curve.png` と `backtest_summary.csv` に保存

### 仮想運用（DRY_RUN）

```bash
MODE=DRY_RUN python main.py
```

- `DEEPSEEK_API_KEY` を設定すると、AI（DeepSeek）による銘柄選定が有効になる
- FRの「一過性スパイク」か「持続トレンド」かを判定し、ENTRY の銘柄のみを選定

### 実運用（LIVE）

```bash
MODE=LIVE python main.py
```

- API キー・シークレットの設定が必須
- 片駆け時は緊急決済ロジックで自動クローズ

## 運用スクリプト (ops.py)

AWS Lambda デプロイ後に、デプロイ済みシステムの状態確認・操作を行う CLI ツール。

**前提条件**

- `pip install boto3` で boto3 をインストール
- `.env` に `DYNAMODB_TABLE` を設定（デプロイ時に作成された DynamoDB テーブル名）
- AWS CLI で認証済み（`aws configure` または環境変数）

| コマンド | 説明 |
|----------|------|
| `python ops.py status` | Lambda モード・EventBridge（デプロイ日時・スケジュール・状態）・ポジション・直近 Z-Score を表示 |
| `python ops.py logs [--days N] [--limit N]` | 過去 N 日間のモニタリング履歴を表示（デフォルト: 7日, 50件） |
| `python ops.py trades [--limit N] [--all]` | トレード履歴と PnL を表示。`--all` で全件 |
| `python ops.py invoke` | Lambda を手動で即時実行（定期実行を待たずに1回分のロジックを実行） |
| `python ops.py stop [-y]` | ポジション状態を NO_POSITION にリセット（緊急停止）。`-y` で確認なし |

**使用例**

```bash
# 状態確認
python ops.py status

# 直近14日間のモニタリングログを20件表示
python ops.py logs --days 14 --limit 20

# Lambda を手動実行
python ops.py invoke

# 緊急停止（確認プロンプトあり）
python ops.py stop
```

**環境変数（オプション）**

| 変数 | 説明 | デフォルト |
|------|------|------------|
| `LAMBDA_FUNCTION_NAME` | Lambda 関数名（invoke 時） | `{SAM_STACK_NAME}-CryptoTrader` |
| `SAM_STACK_NAME` | SAM スタック名 | `ai-crypto-trader` |

## 手数料と収益性

| 項目 | VIP0 | VIP1+ |
|------|------|-------|
| 現物テイカー手数料 | 0.10% | 〜0.06% |
| USDT Perp メイカー手数料 | 0.02% | 〜0.01% |
| 往復コスト（片側合計 × 2） | 〜0.34% | 〜0.14% |
| 損益分岐点 (FR=0.01%/8h) | 〜12日 | 〜5日 |
| 損益分岐点 (FR=0.005%/8h) | 〜23日 | 〜10日 |

**収益性はFR水準に大きく依存**:
- ブル相場（FR 0.01-0.05%/8h）: 高収益
- レンジ相場（FR 0.005%/8h以下）: 手数料とほぼ相殺
- VIPランク向上で手数料が下がり、収益性が大幅改善

## バックテストの注意

- **FR履歴期間**: Bybit の FR 履歴は直近6-8ヶ月分のみ。OHLCV と期間が重なるよう設定すること。
- **市場環境**: レンジ/ベア相場ではFR水準が低く、収益が限定的になる場合がある。

## ディレクトリ構成

```
AI-Crypto-Trader/
├── config.py          # モード切替・設定管理
├── fetcher.py         # データ取得（リアルタイム・過去）
├── screener.py        # 銘柄選定（コスト考慮＋AI判定）
├── executor.py        # 注文執行・ポートフォリオ管理
├── backtester.py      # バックテスト実行
├── main.py            # メインエントリーポイント
├── ops.py             # 運用 CLI（status, logs, trades, invoke, stop）
├── requirements.txt  # 依存パッケージ
├── .env.example       # 環境変数テンプレート
├── scripts/           # デプロイスクリプト等
└── docs/
    ├── spec.md        # 仕様書
    └── design.md      # 実装設計書
```

## 免責事項

本ソフトウェアは教育・研究目的で提供されています。暗号通貨取引にはリスクが伴います。実運用前に十分な検証を行い、自己責任でご利用ください。
