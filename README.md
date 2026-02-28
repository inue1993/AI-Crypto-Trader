# AI Crypto Trader

暗号通貨のデルタニュートラル・ファンディングレート（FR）アービトラージボット。無期限先物のFR歪みを利用し、現物買いと先物売りを同数量発注することで価格変動リスクを排除しつつFR収益を獲得する自動売買システム。

## 特徴

- **デルタニュートラル戦略**: 現物と先物を同数量で保有し、価格変動リスクをヘッジ
- **3つの動作モード**: LIVE（実運用）、DRY_RUN（仮想運用）、BACKTEST（過去検証）
- **対応取引所**: Binance / Bybit（ccxt 経由）

## 必要条件

- Python 3.10 以上

## セットアップ

```bash
# リポジトリのクローン
git clone <repository-url>
cd AI-Crypto-Trader

# 仮想環境の作成と有効化
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 依存関係のインストール
pip install -r requirements.txt

# 環境変数の設定
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
| `INITIAL_CAPITAL` | 初期資金（USD） | 10000 |
| `MAX_POSITIONS` | 最大同時ポジション数 | 3 |
| `MIN_FR_THRESHOLD` | エントリー最低 FR 閾値（0.000005=0.0005%） | 0.000005 |
| `TAKER_FEE` | 取引手数料率 | 0.001 |
| `SLIPPAGE` | 推定スリッページ率 | 0.0005 |
| `BACKTEST_START` | バックテスト開始日 | 2024-01-01 |
| `BACKTEST_END` | バックテスト終了日 | 2024-03-31 |
| `BACKTEST_SYMBOLS` | バックテスト対象銘柄（カンマ区切り） | BTC/USDT,ETH/USDT,SOL/USDT |

## 使い方

### バックテスト（過去シミュレーション）

```bash
MODE=BACKTEST python main.py
```

- 指定期間の OHLCV と FR 履歴を API から取得してシミュレーション
- 結果は `equity_curve.png` と `backtest_summary.csv` に保存

### 仮想運用（DRY_RUN）

```bash
MODE=DRY_RUN python main.py
```

- 実際の発注は行わず、仮想ポートフォリオで運用をシミュレーション
- 1 時間ごとにループ実行

### 実運用（LIVE）

```bash
MODE=LIVE python main.py
```

- 実際に成行注文を発注
- API キー・シークレットの設定が必須
- 片駆け時は緊急決済ロジックで自動クローズ

## ディレクトリ構成

```
AI-Crypto-Trader/
├── config.py          # モード切替・設定管理
├── fetcher.py         # データ取得（リアルタイム・過去）
├── screener.py        # 銘柄選定ロジック
├── executor.py        # 注文執行・ポートフォリオ管理
├── backtester.py      # バックテスト実行
├── main.py            # メインエントリーポイント
├── requirements.txt   # 依存パッケージ
├── .env.example       # 環境変数テンプレート
└── docs/
    ├── spec.md        # 仕様書
    └── design.md      # 実装設計書
```

## バックテスト時の注意

- **期間**: Bybit の FR 履歴は直近数ヶ月分のみ。`BACKTEST_START` / `BACKTEST_END` は OHLCV と FR の取得可能期間が重なるように設定すること（例: 2024-09-24 〜 2024-10-31）。
- **MIN_FR_THRESHOLD**: 閾値を下げると取引が増える。0.000005（0.0005%）程度にするとバックテストで取引が発生しやすい。

## 免責事項

本ソフトウェアは教育・研究目的で提供されています。暗号通貨取引にはリスクが伴います。実運用前に十分な検証を行い、自己責任でご利用ください。
