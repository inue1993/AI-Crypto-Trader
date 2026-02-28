#!/bin/bash
# SAM build が Docker を検出しない場合の代替デプロイスクリプト
# 使い方: ./deploy.sh [exchange]  例: ./deploy.sh bitbank
set -e

STACK_NAME="ai-crypto-trader"
REGION="${AWS_REGION:-ap-northeast-1}"
IMAGE_NAME="ai-crypto-trader-cryptotraderfunction"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# .env を読み込み
[ -f .env ] && set -a && source .env && set +a

# 引数で取引所を指定可能（.env より優先）
if [ -n "${1:-}" ]; then
  case "$1" in
    bitbank|bybit|binance) EXCHANGE="$1" ;;
    *) echo "不明な取引所: $1 (bitbank|bybit|binance)" >&2; exit 1 ;;
  esac
fi

# AWS Account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${IMAGE_NAME}:latest"

echo "=== 1. ECR リポジトリ作成 ==="
aws ecr describe-repositories --repository-names "$IMAGE_NAME" --region "$REGION" 2>/dev/null || \
  aws ecr create-repository --repository-name "$IMAGE_NAME" --region "$REGION"

echo "=== 2. ECR ログイン ==="
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "=== 3. イメージをビルド・タグ付け・プッシュ ==="
docker build --no-cache --platform linux/amd64 --provenance=false --sbom=false -t cryptotrader-lambda:latest .
docker tag cryptotrader-lambda:latest "$ECR_URI"
docker push "$ECR_URI"

echo "=== 4. パッケージ済みテンプレート作成 ==="
python3 - "$ECR_URI" << 'PYEOF'
import sys
ecr_uri = sys.argv[1]
with open("template.yaml") as f:
    content = f.read()
# CryptoTraderFunction の Properties に ImageUri を追加
if "ImageUri:" not in content:
    content = content.replace(
        "      Policies:",
        f"      ImageUri: {ecr_uri}\n      Policies:"
    )
with open("packaged.yaml", "w") as f:
    f.write(content)
PYEOF

echo "=== 5. SAM デプロイ ==="
echo "取引所: ${EXCHANGE:-bybit} | モード: ${MODE:-DRY_RUN}"
# bitbank の場合は初期資金を円建てでデフォルト 100万円
DEFAULT_CAPITAL="10000"
[ "${EXCHANGE:-bybit}" = "bitbank" ] && DEFAULT_CAPITAL="${INITIAL_CAPITAL:-1000000}"
# 空のパラメータは省略（SAM は空値を嫌う）
PARAMS="Mode=${MODE:-DRY_RUN} Exchange=${EXCHANGE:-bybit} InitialCapital=${INITIAL_CAPITAL:-$DEFAULT_CAPITAL}"
[ -n "${API_KEY:-}" ] && PARAMS="$PARAMS ApiKey=$API_KEY"
[ -n "${API_SECRET:-}" ] && PARAMS="$PARAMS ApiSecret=$API_SECRET"
[ -n "${DEEPSEEK_API_KEY:-}" ] && PARAMS="$PARAMS DeepSeekApiKey=$DEEPSEEK_API_KEY"
[ -n "${CRYPTOPANIC_API_KEY:-}" ] && PARAMS="$PARAMS CryptoPanicApiKey=$CRYPTOPANIC_API_KEY"
[ -n "${SLACK_WEBHOOK_URL:-}" ] && PARAMS="$PARAMS SlackWebhookUrl=$SLACK_WEBHOOK_URL"

sam deploy \
  --template-file packaged.yaml \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides $PARAMS \
  --resolve-image-repos \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset

echo "=== 6. Lambda イメージ強制更新 ==="
# CloudFormation は ImageUri が同じだと更新を検知しないため、明示的に update-function-code で最新イメージを反映
FUNC_NAME="${STACK_NAME}-CryptoTrader"
aws lambda update-function-code \
  --function-name "$FUNC_NAME" \
  --image-uri "$ECR_URI" \
  --region "$REGION" \
  --output text --query 'LastModified' 2>/dev/null && echo "Lambda イメージを更新しました" || echo "Lambda 更新スキップ（初回デプロイ時は無視可）"

echo ""
echo "=== デプロイ完了 ==="
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --query 'Stacks[0].Outputs' --output table 2>/dev/null || true
