# プロジェクト概要：AI駆動型ペアトレード（統計的裁定取引）ボット

## 1. 目的
相関性の高い2銘柄（BTC/USDT と ETH/USDT）の価格比率の統計的乖離を突き、平均回帰を狙うペアトレード戦略により、利益幅が大きく手数料負けしにくい自動売買システムを構築する。
本システムは、実運用（LIVE）、仮想運用（DRY RUN）、および過去検証（BACKTEST）の3つのモードを備える。

## 2. 技術スタック
- **言語:** Python 3.10以上
- **主要ライブラリ:**
  - `ccxt` (取引所API通信)
  - `pandas`, `numpy` (データ処理・計算)
  - `python-dotenv` (環境変数管理)
  - `matplotlib` (バックテスト結果のグラフ描画)
- **対象取引所:** Binance または Bybit

## 3. 戦略のコアロジック（Z-Scoreベースの平均回帰）

### 3.1 対象銘柄
- 固定ペア: **BTC/USDT** と **ETH/USDT**

### 3.2 Z-Score 計算
- 過去データ（1時間足OHLCVの過去200期間）から、2銘柄の終値の **比率（Ratio = ETH価格 / BTC価格）** を計算
- Ratio の **移動平均（Rolling Mean）** と **標準偏差（Rolling Std）** を計算
- **Z-Score = (Current_Ratio - Rolling_Mean) / Rolling_Std**

### 3.3 エントリー・エグジットルール
- **エントリー条件:**
  - `Z-Score < -2.0`: ETHがBTCに対して異常に売られている → ETHロング（現物買い）、BTCショート（先物売り）
  - `Z-Score > 2.0`: ETHがBTCに対して異常に買われている → ETHショート（先物売り）、BTCロング（現物買い）
- **エグジット条件:** Z-Score が 0（平均値）に戻ったタイミングで両ポジションを決済
- **ハード・ストップロス:** Z-Score が ±3.5 を超えて逆行した場合、平均回帰を諦めて即座に損切り
- **取引コスト:** エントリー・エグジットそれぞれで、各銘柄に 0.15%（往復で計 0.3% × 2銘柄分）を控除

## 4. AI（DeepSeek）によるファンダメンタルズ・フィルター
Z-Score の異常値検知時、即座に発注せず AI フィルターを通す。

- **DRY_RUN用プロンプト（ニュース注入）:** 「最新ニュース一覧を読み、ハッキングや規制強化などの悪材料が含まれている場合は絶対に PASS、悪材料が見当たらないノイズの場合のみ ENTRY」
- **バックテスト用プロンプト:** 価格データのみ（24h騰落率）で判定
- **ニュース取得:** CryptoPanic API（CRYPTOPANIC_API_KEY）または RSS（CoinDesk, CoinTelegraph）フォールバック
- **AI モード切替:** DEEPSEEK_API_KEY 設定時は本物のAPI、未設定時はモック（常に ENTRY）
- **エントリー条件:** decision=="ENTRY" かつ confidence > 70

## 5. 期間指定バックテスト
- **BACKTEST_START / BACKTEST_END** で対象期間を指定可能（API コスト節約）
- 例: 死の谷期間 `2026-01-25` 〜 `2026-02-15`

## 6. バックテスト仕様
- **初期資金:** $10,000
- **ポジションサイズ:** 資金の 50%（各銘柄 25% ずつ）
- **出力:**
  - Z-Score 推移のサブグラフ
  - エクイティカーブ（残高推移）のメイングラフ
  - 上下に並べて matplotlib で描画

## 7. 動作モード（Modes）
1. **LIVE:** 実際の発注APIで運用。Lambda + EventBridge で1時間ごとに起動。ポジション状態は DynamoDB で永続化
2. **DRY_RUN:** 1時間ごとにZスコア監視、エントリー条件（|Z|≥2.0）時にニュース取得＋AI判定をログ出力（発注なし）
3. **BACKTEST:** 過去OHLCVデータで戦略シミュレーション（ローカル実行）

## 8. AWS デプロイ構成

- **Lambda**: Docker イメージベース。1時間ごとに EventBridge で起動
- **DynamoDB**: ポジション状態・モニタリングログ・トレード履歴を Single Table Design で保存。TTL 90日
- **Slack**: シグナル検出・エントリー/エグジット時に Webhook 通知
- **SAM**: `template.yaml` + `Dockerfile` でデプロイ。`sam build && sam deploy --guided`

## 9. 運用手順（デプロイ後）

- **status**: `python ops.py status` でポジション状態・直近 Z-Score 確認
- **logs**: `python ops.py logs --days 7` でモニタリング履歴
- **trades**: `python ops.py trades` でトレード履歴・PnL
- **invoke**: `python ops.py invoke` で Lambda を手動実行
- **stop**: `python ops.py stop` でポジション状態を NO_POSITION にリセット（緊急時）
- **モード切替**: `sam deploy --parameter-overrides Mode=LIVE` で LIVE 昇格

## 10. 成果物（ディレクトリ構成）
- `config.py` - モード切替・設定管理
- `fetcher.py` - OHLCV 等のデータ取得
- `screener.py` - Z-Score 計算・AI判定（ペアトレード用）
- `executor.py` - 注文執行・ペアトレード（open_pair_trade, close_pair_trade）
- `backtester.py` - ペアトレードバックテスト・グラフ出力
- `main.py` - エントリーポイント（run_once ステートマシン）
- `storage.py` - DynamoDB 永続化レイヤー
- `notifier.py` - Slack Webhook 通知
- `lambda_handler.py` - Lambda エントリーポイント
- `ops.py` - 運用 CLI（status, logs, trades, invoke, stop）
- `template.yaml`, `Dockerfile`, `samconfig.toml` - SAM デプロイ
- `.env.example`, `requirements.txt`
