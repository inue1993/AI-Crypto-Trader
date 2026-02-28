# 実装設計書（ペアトレード版）

本ドキュメントは `docs/spec.md` に基づく実装の詳細を記述する。

## 1. ディレクトリ構成

```
AI-Crypto-Trader/
├── config.py          # モード切替・設定管理
├── fetcher.py         # OHLCV データ取得
├── screener.py        # Z-Score計算・AI判定（ペアトレード用）
├── executor.py        # 注文執行・ペアトレード（open_pair_trade, close_pair_trade）
├── backtester.py      # ペアトレードバックテスト・グラフ出力
├── main.py            # メインエントリーポイント（run_once ステートマシン）
├── storage.py         # DynamoDB 永続化レイヤー
├── notifier.py        # Slack Webhook 通知
├── lambda_handler.py  # Lambda エントリーポイント
├── ops.py             # 運用 CLI
├── template.yaml      # SAM テンプレート
├── Dockerfile         # Lambda コンテナ
├── samconfig.toml     # SAM デプロイ設定
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
  - `mode`, `exchange`, `api_key`, `api_secret`, `deepseek_api_key`
  - `initial_capital`, `position_size_pct` (50%)
  - `backtest_start`, `backtest_end`
  - `pair_symbols`: 取引所に応じて ["BTC/USDT", "ETH/USDT"] または ["BTC/JPY", "ETH/JPY"]（bitbank）
  - `z_score_entry_threshold`: 2.0
  - `z_score_exit_threshold`: 0.0
  - `rolling_window`: 200
  - `transaction_cost_rate`: 0.0015 (0.15% per side)

### 2.2 fetcher.py

- **DataFetcher**: ccxt を用いたデータ取得。bitbank は先物非対応のため現物のみ
- **fetch_crypto_news(limit=10)**: ETH/BTC関連ニュースを取得
  - CryptoPanic API（CRYPTOPANIC_API_KEY 設定時）
  - RSS フォールバック（CoinDesk, CoinTelegraph、BTC/ETH キーワードでフィルタ）
  - `fetch_ohlcv()`: 1時間足OHLCV取得（単発）
  - `fetch_ohlcv_range()`: 指定期間のOHLCVを取得。bitbank は Candlestick API が YYYYMMDD 指定で1日分しか返さないため日単位でループ。Bybit/Binance はカーソルベースでループ
  - 既存の `get_tradable_symbols`, `get_funding_rates` 等は LIVE/DRY_RUN 用に残す

### 2.3 screener.py（ペアトレード版）

- **定数:**
  - `TRANSACTION_COST_RATE = 0.0015` (0.15% per side)
  - `Z_SCORE_ENTRY_THRESHOLD = 2.0`
  - `Z_SCORE_EXIT_THRESHOLD = 0.5` (平均回帰: |Z|≤0.5 でエグジット)
  - `Z_SCORE_STOP_LOSS = 3.5` (ハード・ストップロス: |Z|>3.5 で即座に損切り)
  - `ROLLING_WINDOW = 200`
- **PairTradeScreener**:
  - `calc_z_score(eth_close, btc_close, window)`: Ratio, Rolling Mean, Std, Z-Score を計算
  - `check_entry_signal(z_score)`: Z-Score が ±2.0 を超え、かつ |Z|≤3.5 の範囲か判定（ストップロス域ではエントリー禁止）
  - `check_exit_signal(z_score)`: Z-Score が 0 に戻ったか判定
  - `check_stop_loss(z_score, direction)`: |Z|>3.5 で即座に損切り
  - **AI判定**（DeepSeek）:
    - システムプロンプト: 2シグマ乖離がファンダメンタルズ変化かノイズかを判定
    - `ai_decision(..., news_titles)`: DeepSeek API 呼び出し
    - **news_titles あり（DRY_RUN）**: ニュースベースのプロンプト（悪材料→PASS、ノイズ→ENTRY）
    - **news_titles なし（バックテスト）**: 価格データのみのプロンプト
    - DEEPSEEK_API_KEY 設定時: 本物のAPI、未設定時: モック（常に ENTRY）
    - エントリー条件: decision=="ENTRY" かつ confidence > 70
    - API エラー時: PASS（安全側）、time.sleep(1) でレートリミット回避

### 2.4 backtester.py（ペアトレード版）

- **BacktestResult**:
  - `equity_curve`, `timestamps`, `z_scores`
  - `total_trades`, `winning_trades`, `total_costs`, `max_drawdown`
- **Backtester**:
  - `load_data()`: BTC/USDT, ETH/USDT（または BTC/JPY, ETH/JPY）の1時間足OHLCVを取得。`fetch_ohlcv_range` を使用（bitbank は日単位ループで250件以上取得）
  - `run()`: 時系列シミュレーション
    - 各タイムスタンプで Ratio, Z-Score を計算
    - エントリー: Z-Score < -2.0 または > 2.0 かつ AI が ENTRY
    - エグジット: Z-Score が 0 に戻ったタイミング
    - 取引コスト: 0.15% × 2銘柄 × 2（エントリー＋エグジット）= 0.6% 往復
    - ポジションサイズ: 資金の 50%（各銘柄 25%）
  - `plot_equity_curve()`: Z-Score サブグラフ + エクイティカーブ メイングラフを上下に並べて描画
  - `export_summary()`: CSV 出力

### 2.5 main.py

- **run_once()**: 1回分の監視・取引ロジック。DRY_RUN/LIVE 共通のステートマシン。storage/notifier を渡すと DynamoDB 保存・Slack 通知
- **run_backtest()**: BACKTEST モードでペアトレードバックテスト実行
- **run_dry_run_loop()**: DRY_RUN モード（1時間ごとに run_once をループ）

### 2.6 storage.py

- **DynamoDBStorage**: DynamoDB Single Table Design
  - `load_position_state()` / `save_position_state()`: ポジション状態
  - `save_monitor_log()`: 毎時モニタリング結果
  - `save_signal()`: シグナルイベント
  - `save_trade()`: トレード履歴
  - `query_recent_monitors()` / `query_trades()`: クエリ
  - `reset_position_state()`: 緊急リセット

### 2.7 notifier.py

- **SlackNotifier**: Slack Incoming Webhook
  - `send_signal_alert()`: シグナル検出
  - `send_entry_alert()`: エントリー実行（LIVE）
  - `send_exit_alert()`: エグジット実行（LIVE）

## 3. データフロー（バックテスト）

```
main.py
  └─ BACKTEST
        ├─ Backtester.load_data() → BTC/USDT, ETH/USDT の OHLCV
        ├─ Backtester.run()
        │     ├─ 各 ts で Ratio = ETH_close / BTC_close
        │     ├─ Rolling Mean, Std, Z-Score 計算
        │     ├─ Z-Score < -2 or > 2 → AI判定（モック: ENTRY）
        │     ├─ エントリー: Long ETH + Short BTC (or 逆)
        │     ├─ Z-Score → 0 でエグジット
        │     └─ 取引コスト 0.15% × 各銘柄 × 往復
        ├─ plot_equity_curve() → Z-Score + Equity グラフ
        └─ export_summary()
```

## 4. 取引コストの内訳

- エントリー時: ETH 0.15%, BTC 0.15% (計 0.3%)
- エグジット時: ETH 0.15%, BTC 0.15% (計 0.3%)
- 往復合計: 0.6% (手数料＋スプレッド想定)

## 5. AWS デプロイ・ストレージ

### 5.1 DynamoDB テーブル設計（Single Table）

| pk    | sk           | 用途                 |
|-------|--------------|----------------------|
| STATE | position     | 現在のオープンポジション |
| MONITOR | ISO8601    | 毎時モニタリング結果   |
| SIGNAL | ISO8601     | シグナル検出イベント   |
| TRADE | ISO8601      | 完了したトレード記録   |

TTL 90日で自動削除（STATE は TTL なし）。

### 5.2 LIVE ステートマシン

Lambda 毎時起動時: NO_POSITION → エントリー判定（AI含む）→ 条件合致で注文 → OPEN。OPEN → エグジット/ストップロス判定 → 条件合致で決済 → NO_POSITION。

### 5.3 運用手順

- `python ops.py status`: Lambda MODE・**EventBridge（deployed_at, schedule, state）**・ポジション・直近 Z-Score を表示
- `python ops.py logs` / `trades [--all]`（全件表示）/ `invoke` / `stop`
- `sam deploy --parameter-overrides Mode=LIVE` で LIVE 昇格
