"""海外公开行情适配器：CoinGecko 优先，Hyperliquid 作为线上备用源。"""
from __future__ import annotations

import json
import ssl
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

COINGECKO = "https://api.coingecko.com/api/v3"
HYPERLIQUID = "https://api.hyperliquid.xyz/info"
_CACHE: dict[str, tuple[float, object]] = {}
TTL_SECONDS = 180
COINS = [
    ("bitcoin", "BTC", "Bitcoin"), ("ethereum", "ETH", "Ethereum"),
    ("solana", "SOL", "Solana"), ("ripple", "XRP", "XRP"),
    ("dogecoin", "DOGE", "Dogecoin"), ("cardano", "ADA", "Cardano"),
    ("avalanche-2", "AVAX", "Avalanche"), ("chainlink", "LINK", "Chainlink"),
    ("sui", "SUI", "Sui"), ("polkadot", "DOT", "Polkadot"),
    ("uniswap", "UNI", "Uniswap"), ("aptos", "APT", "Aptos"),
]
COIN_BY_ID = {x[0]: x for x in COINS}


def _cached(key: str, loader):
    now = time.time()
    item = _CACHE.get(key)
    if item and now - item[0] < TTL_SECONDS:
        return item[1]
    value = loader()
    _CACHE[key] = (now, value)
    return value


def _get(path: str, params: dict[str, object]) -> object:
    def load():
        req = Request(COINGECKO + path + "?" + urlencode(params), headers={"User-Agent": "huiche-guance-agent/1.0", "Accept": "application/json"})
        with urlopen(req, timeout=18, context=ssl.create_default_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    return _cached("cg:" + path + "?" + urlencode(params), load)


def _hyper(request_body: dict) -> object:
    def load():
        req = Request(HYPERLIQUID, data=json.dumps(request_body).encode(), headers={"Content-Type": "application/json", "User-Agent": "huiche-guance-agent/1.0"}, method="POST")
        with urlopen(req, timeout=18) as response:
            return json.loads(response.read().decode("utf-8"))
    return _cached("hl:" + json.dumps(request_body, sort_keys=True), load)


def _candles(symbol: str, days: int = 365) -> list[dict]:
    start = int((time.time() - days * 86400) * 1000)
    data = _hyper({"type": "candleSnapshot", "req": {"coin": symbol, "interval": "1d", "startTime": start}})
    return data if isinstance(data, list) else []


def _fallback_overview(limit: int) -> list[dict]:
    result = []
    for coin_id, symbol, name in COINS[:limit]:
        candles = _candles(symbol)
        if not candles:
            continue
        closes = [float(x.get("c", 0)) for x in candles if x.get("c")]
        if not closes:
            continue
        price, ath = closes[-1], max(float(x.get("h", x.get("c", 0))) for x in candles)
        result.append({"id": coin_id, "symbol": symbol.lower(), "name": name, "image": "", "current_price": price, "ath": ath, "ath_date": {}, "market_cap": 0, "price_change_percentage_24h": (price / closes[-2] - 1) * 100 if len(closes) > 1 and closes[-2] else 0})
    return result


def market_overview(limit: int = 20) -> list[dict]:
    try:
        data = _get("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": limit, "page": 1, "sparkline": "false", "price_change_percentage": "24h,7d"})
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return _fallback_overview(min(limit, len(COINS)))


def history(coin_id: str, days: int = 365) -> dict:
    try:
        data = _get(f"/coins/{coin_id}/market_chart", {"vs_currency": "usd", "days": days, "interval": "daily"})
        if isinstance(data, dict) and data.get("prices"):
            return data
    except Exception:
        pass
    symbol = COIN_BY_ID.get(coin_id, (coin_id, coin_id.upper(), coin_id))[1]
    candles = _candles(symbol, days)
    prices = [[x.get("t", 0), float(x.get("c", 0))] for x in candles if x.get("c")]
    volumes = [[x.get("t", 0), float(x.get("v", 0))] for x in candles if x.get("v")]
    return {"prices": prices, "total_volumes": volumes}
