"""bitbank 信用取引用 API クライアント。ccxt がマージン未対応のため直接 REST API を呼ぶ。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

BITBANK_API_BASE = "https://api.bitbank.cc/v1"


def _to_bitbank_pair(symbol: str) -> str:
    """ccxt シンボル (BTC/JPY) を bitbank API 形式 (btc_jpy) に変換。"""
    return symbol.replace("/", "_").lower()


def _sign(secret: str, nonce: str, path: str, body: Optional[str] = None) -> str:
    """bitbank API 署名を生成。"""
    msg = nonce + (body or "") + path
    return hmac.new(
        secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _request(
    api_key: str,
    api_secret: str,
    method: str,
    path: str,
    body: Optional[dict] = None,
) -> dict[str, Any]:
    """bitbank Private API を呼び出す。"""
    nonce = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(",", ":")) if body else None
    # bitbank 署名: GET は nonce+path, POST は nonce+body（path は /v1/... 形式）
    if method == "GET":
        sig_path = path if path.startswith("/v1") else f"/v1{path}" if path.startswith("/") else f"/v1/{path}"
        sig_msg = nonce + sig_path
    else:
        sig_msg = nonce + (body_str or "")
    signature = hmac.new(
        api_secret.encode("utf-8"),
        sig_msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    url = f"{BITBANK_API_BASE}{path}" if path.startswith("/") else f"{BITBANK_API_BASE}/{path}"
    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-NONCE": nonce,
        "ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        url,
        data=body_str.encode("utf-8") if body_str else None,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("success"):
        raise RuntimeError(f"bitbank API error: {data}")
    return data.get("data", {})


def create_margin_order(
    api_key: str,
    api_secret: str,
    symbol: str,
    side: str,
    amount: float,
    position_side: str,
    order_type: str = "market",
) -> dict[str, Any]:
    """bitbank 信用取引の成行注文を発注する。

    Args:
        api_key: API キー
        api_secret: API シークレット
        symbol: 銘柄 (BTC/JPY, ETH/JPY)
        side: "buy" | "sell"
        amount: 数量（base 通貨）
        position_side: "long" | "short"
        order_type: "market" | "limit"

    Returns:
        注文結果
    """
    pair = _to_bitbank_pair(symbol)
    body: dict[str, Any] = {
        "pair": pair,
        "amount": str(amount),
        "side": side,
        "type": order_type,
        "position_side": position_side,
    }
    return _request(api_key, api_secret, "POST", "/user/spot/order", body)
