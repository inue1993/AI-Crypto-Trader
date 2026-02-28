# Lambda 用 Docker イメージ（x86_64 必須）
FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.12

# 依存関係をインストール
COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt

# アプリケーションコードをコピー
COPY config.py fetcher.py screener.py executor.py main.py storage.py notifier.py lambda_handler.py bitbank_client.py ${LAMBDA_TASK_ROOT}/
COPY backtester.py ${LAMBDA_TASK_ROOT}/

# Lambda ハンドラーを指定
CMD ["lambda_handler.handler"]
