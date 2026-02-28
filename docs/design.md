# 実装設計書

本ドキュメントは `docs/spec.md` に基づく実装の詳細を記述する。

## 1. ディレクトリ構成

```
AI-Crypto-Trader/
├── config.py          # モード切替・設定管理
├── fetcher.py         # リアルタイム・過去データ取得
├── screener.py        # 銘柄選定ロジック（AIモック）
├── executor.py        # 注文執行・ポートフォリオ管理
├── backtester.py      # バックテスト実行・結果出力
├── main.py            # メインエントリーポイント
├── .env.example       # 環境変数テンプレート
├── requirements.txt   # 依存パッケージ
└── docs/
    ├── spec.md        # 仕様書
    └── design.md      # 本設計書
```

## 2. モジュール詳細

### 2.1 config.py

- **Mode**: Enum (LIVE / DRY_RUN / BACKTEST)
- **Config**: dataclass
  - `mode`, `exchange`, `api_key`, `api_secret`
  - `initial_capital`, `max_positions`, `min_fr_threshold`
  - `taker_fee`, `slippage`
  - `backtest_start`, `backtest_end`, `backtest_symbols`
- **Config.from_env()**: `.env` から設定を読み込む

### 2.2 fetcher.py

- **DataFetcher**: ccxt を用いたデータ取得
  - `get_tradable_symbols()`: 現物＋先物両方で上場している銘柄リスト
  - `get_funding_rates(symbols)`: 各銘柄の現在FR
  - `get_tickers(symbols)`: 価格・出来高
  - `get_orderbook(symbol, limit)`: オーダーブック
  - `fetch_ohlcv()`, `fetch_funding_rate_history()`: 過去データ
  - `get_market_data()`: Step 1 用一括取得

### 2.3 screener.py

- **Screener**: 銘柄選定
  - `screen(market_data)`: 出来高過少・FRマイナス除外
  - `select_top(candidates, top_n)`: FR上位N銘柄選定（AIモック）
  - `run(market_data)`: 一括実行

### 2.4 executor.py

- **Portfolio**: ポジション管理
  - `positions`, `balance`, `trade_history`
  - `add_position()`, `close_position()`, `record_funding()`
- **Executor**: 注文執行
  - DRY_RUN: 仮想発注、Portfolio に記録
  - LIVE: ccxt で実発注、片駆け時は緊急決済
  - `open_delta_neutral()`, `close_delta_neutral()`
  - `check_and_collect_funding()`: FR収益計上

### 2.5 backtester.py

- **Backtester**: バックテスト
  - `load_data()`: API または CSV から OHLCV + FR 履歴取得
  - `run()`: 時系列シミュレーション
  - `calculate_metrics()`: 勝率、最大DD、累積FR、総コスト
  - `plot_equity_curve()`: matplotlib でエクイティカーブ
  - `export_summary()`: CSV 出力

### 2.6 main.py

- **run_backtest()**: BACKTEST モード
- **run_live_loop()**: LIVE / DRY_RUN の 1 時間ループ
  - データ取得 → スクリーニング → ポジション管理 → FR収益計上 → 終了条件チェック

## 3. データフロー

```
main.py
  ├─ BACKTEST → Backtester.load_data() → run() → plot + export
  └─ LIVE/DRY_RUN → ループ
        ├─ DataFetcher.get_market_data()
        ├─ Executor.check_and_collect_funding()
        ├─ FR < 閾値 → Executor.close_delta_neutral()
        ├─ Screener.run() → 選定銘柄
        └─ Executor.open_delta_neutral()（空きスロット分）
```

## 4. バックテスト設定の注意

- `BACKTEST_START` / `BACKTEST_END`: Bybit の FR 履歴は直近のみのため、OHLCV と期間が重なるよう設定する。
- `MIN_FR_THRESHOLD`: 0.000005 程度にするとバックテストで取引が発生しやすい。

## 5. 環境変数

`.env.example` 参照。主要項目:

- `MODE`: LIVE | DRY_RUN | BACKTEST
- `EXCHANGE`: binance | bybit
- `API_KEY`, `API_SECRET`
- `INITIAL_CAPITAL`, `MAX_POSITIONS`, `MIN_FR_THRESHOLD`
- `BACKTEST_START`, `BACKTEST_END`, `BACKTEST_SYMBOLS`
