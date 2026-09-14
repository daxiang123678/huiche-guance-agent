"""行情数据适配层：币安官方公开资源优先，综合平台数据兜底。

取数优先级（从上往下依次尝试，任何一层失败都如实降级到下一层，绝不编造数据）：

    1. 币安官方公开行情接口  https://data-api.binance.vision   —— 免密钥，官方口径
    2. CoinGecko 公共接口     https://api.coingecko.com/api/v3 —— 综合平台口径
    3. Hyperliquid 公开接口   https://api.hyperliquid.xyz/info —— 综合平台口径，最后兜底

实际用了哪一层，会记录在 LAST_SOURCE 里，页面和接口都会如实展示。
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode

# --------------------------------------------------------------------------
# 数据源定义
# --------------------------------------------------------------------------

BINANCE_API = "https://data-api.binance.vision"
COINGECKO = "https://api.coingecko.com/api/v3"
HYPERLIQUID = "https://api.hyperliquid.xyz/info"

SOURCE_LABELS = {
    "binance": "币安官方公开行情接口",
    "coingecko": "CoinGecko 公共接口",
    "hyperliquid": "Hyperliquid 公开接口",
}

# 显式忽略系统代理。
# 原因：本机实测发现系统代理开关经常和代理软件的实际状态不一致
# （注册表里 ProxyEnable=1，但代理进程没在监听），一旦走了那个不存在的代理，
# 所有请求都会失败；而这两个官方数据域直连是可通的。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

USER_AGENT = "huiche-guance-agent/1.0"

_CACHE: dict[str, tuple[float, object]] = {}
TTL_SECONDS = 180          # 行情快照缓存 3 分钟
ATH_TTL_SECONDS = 3600     # 历史最高价不会变，缓存久一点

# 稳定币与法币不参与回撤排行
STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "AEUR", "USD1", "XUSD",
    "EUR", "EURI", "TRY", "BRL", "ARS", "JPY", "ZAR", "GBP", "USDE", "USDY",
}
LEVERAGED_SUFFIX = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

# 综合平台口径的主流币清单（仅在币安通道不可用时使用）
COINS = [
    ("bitcoin", "BTC", "Bitcoin"), ("ethereum", "ETH", "Ethereum"),
    ("solana", "SOL", "Solana"), ("ripple", "XRP", "XRP"),
    ("dogecoin", "DOGE", "Dogecoin"), ("cardano", "ADA", "Cardano"),
    ("avalanche-2", "AVAX", "Avalanche"), ("chainlink", "LINK", "Chainlink"),
    ("sui", "SUI", "Sui"), ("polkadot", "DOT", "Polkadot"),
    ("uniswap", "UNI", "Uniswap"), ("aptos", "APT", "Aptos"),
]
COIN_BY_ID = {x[0]: x for x in COINS}

# 最近一次实际使用的数据源（页面会读它做展示）
LAST_SOURCE: dict[str, str] = {"overview": "", "history": ""}


def source_label(key: str) -> str:
    return SOURCE_LABELS.get(key or "", "未知来源")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _cached(key: str, loader, ttl: int = TTL_SECONDS):
    now = time.time()
    item = _CACHE.get(key)
    if item and now - item[0] < ttl:
        return item[1]
    value = loader()
    _CACHE[key] = (now, value)
    return value


def _http_json(url: str, timeout: int = 20, post_body: dict | None = None) -> object:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if post_body is not None:
        data = json.dumps(post_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=headers,
        method="POST" if post_body is not None else "GET")
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm_ts(value) -> int:
    """时间戳统一成毫秒。

    实测坑：官方历史 CSV 在不同年份用了不同精度（早期是毫秒 13 位，较新的出现过微秒 16 位），
    所以按量级自适应，不能写死。
    """
    x = int(float(value))
    while x > 10 ** 14:
        x //= 1000
    return x


def _ts_month(ms: int) -> str:
    return time.strftime("%Y-%m", time.gmtime(ms / 1000))


def _is_leveraged(base: str) -> bool:
    """杠杆代币判定，带长度保护（否则 JUP 这类正常币会被误杀）。"""
    for suffix in LEVERAGED_SUFFIX:
        if base.endswith(suffix) and len(base[: -len(suffix)]) >= 2:
            return True
    return False


def _resolve_symbol(text: str) -> str | None:
    """把 btc / BTC / BTCUSDT / bitcoin 都归一到币安交易对。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw in COIN_BY_ID:                     # 综合平台 id，例如 bitcoin
        return COIN_BY_ID[raw][1] + "USDT"
    upper = raw.upper().replace("_", "").replace("-", "")
    if not upper:
        return None
    if upper.endswith("USDT"):
        return upper
    if upper.isascii() and upper.isalpha() and len(upper) <= 12:
        return upper + "USDT"
    return None


# --------------------------------------------------------------------------
# 币安官方公开行情接口
# --------------------------------------------------------------------------

def _binance_symbols() -> list[dict]:
    """全市场 24h 快照（一次请求拿全），过滤后按成交额降序。"""
    rows = _cached("bn:ticker24hr",
                   lambda: _http_json(BINANCE_API + "/api/v3/ticker/24hr", timeout=40),
                   60)
    picked: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        symbol = str(row.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLE_BASES or _is_leveraged(base) or base.isdigit():
            continue
        picked.append(row)
    picked.sort(key=lambda r: float(r.get("quoteVolume") or 0), reverse=True)
    return picked


def _binance_ath(symbol: str) -> dict:
    """用官方月线算真实历史最高价（月线的最高价列取最大值即为全历史 ATH）。"""

    def load():
        safe = quote(symbol, safe="")
        rows = _http_json(
            f"{BINANCE_API}/api/v3/klines?symbol={safe}&interval=1M&limit=1000",
            timeout=25)
        best_high, best_ts = -1.0, 0
        for row in rows if isinstance(rows, list) else []:
            try:
                high = float(row[2])
                ts = _norm_ts(row[0])
            except (IndexError, TypeError, ValueError):
                continue
            if high > best_high:
                best_high, best_ts = high, ts
        if best_high <= 0:
            raise RuntimeError("官方月线没有返回可用数据")
        return {"ath": best_high, "ath_month": _ts_month(best_ts)}

    return _cached("bn:ath:" + symbol, load, ATH_TTL_SECONDS)


def _binance_overview(limit: int) -> list[dict]:
    picked = _binance_symbols()[:limit]
    if not picked:
        raise RuntimeError("官方接口没有返回可用的 USDT 交易对")

    def work(row):
        symbol = str(row.get("symbol"))
        try:
            return symbol, _binance_ath(symbol)
        except Exception:  # noqa: BLE001
            return symbol, {}

    ath_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for symbol, info in pool.map(work, picked):
            ath_map[symbol] = info

    out: list[dict] = []
    for row in picked:
        symbol = str(row.get("symbol"))
        base = symbol[:-4]
        info = ath_map.get(symbol, {})
        out.append({
            "id": base.lower(),
            "symbol": base,
            "name": base,
            "image": "",
            "current_price": float(row.get("lastPrice") or 0),
            "ath": float(info.get("ath") or 0),
            "ath_date": {"usd": info.get("ath_month")},
            "market_cap": None,          # 官方现货快照没有市值字段，如实留空
            "price_change_percentage_24h": float(row.get("priceChangePercent") or 0),
            "quote_volume": float(row.get("quoteVolume") or 0),
            "_source": "binance",
        })
    return out


def _binance_daily(symbol: str, days: int) -> dict:
    """官方日线 -> 自研引擎认得的形状（收盘价 + 成交额）。"""
    safe = quote(symbol, safe="")
    rows = _cached(
        f"bn:klines1d:{symbol}:{days}",
        lambda: _http_json(
            f"{BINANCE_API}/api/v3/klines?symbol={safe}&interval=1d&limit={days}",
            timeout=25),
        300)
    prices: list[list] = []
    volumes: list[list] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            ts = _norm_ts(row[0])
            prices.append([ts, float(row[4])])
            volumes.append([ts, float(row[7])])
        except (IndexError, TypeError, ValueError):
            continue
    if not prices:
        raise RuntimeError("官方日线没有返回可用数据")
    return {"prices": prices, "total_volumes": volumes, "_source": "binance"}


# --------------------------------------------------------------------------
# 综合平台数据源（回退用）
# --------------------------------------------------------------------------

def _get(path: str, params: dict) -> object:
    query = urlencode(params)
    return _cached("cg:" + path + "?" + query,
                   lambda: _http_json(COINGECKO + path + "?" + query))


def _hyper(request_body: dict) -> object:
    return _cached("hl:" + json.dumps(request_body, sort_keys=True),
                   lambda: _http_json(HYPERLIQUID, timeout=18, post_body=request_body))


def _candles(symbol: str, days: int = 365) -> list[dict]:
    start = int((time.time() - days * 86400) * 1000)
    data = _hyper({"type": "candleSnapshot",
                   "req": {"coin": symbol, "interval": "1d", "startTime": start}})
    return data if isinstance(data, list) else []


def _hyper_overview(limit: int) -> list[dict]:
    result = []
    for coin_id, symbol, name in COINS[:limit]:
        candles = _candles(symbol)
        if not candles:
            continue
        closes = [float(x.get("c", 0)) for x in candles if x.get("c")]
        if not closes:
            continue
        price = closes[-1]
        ath = max(float(x.get("h", x.get("c", 0))) for x in candles)
        change = (price / closes[-2] - 1) * 100 if len(closes) > 1 and closes[-2] else 0
        result.append({
            "id": coin_id, "symbol": symbol.lower(), "name": name, "image": "",
            "current_price": price, "ath": ath, "ath_date": {}, "market_cap": None,
            "price_change_percentage_24h": change, "_source": "hyperliquid",
        })
    if not result:
        raise RuntimeError("Hyperliquid 没有返回可用数据")
    return result


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def market_overview(limit: int = 20) -> list[dict]:
    """回撤榜数据。币安官方优先，失败逐级降级，并在 LAST_SOURCE 记录实际来源。"""
    try:
        rows = _binance_overview(limit)
        if rows:
            LAST_SOURCE["overview"] = "binance"
            return rows
    except Exception:  # noqa: BLE001
        pass

    try:
        data = _get("/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": limit,
            "page": 1, "sparkline": "false", "price_change_percentage": "24h,7d"})
        if isinstance(data, list) and data:
            for item in data:
                item["_source"] = "coingecko"
                item.setdefault("quote_volume", None)
            LAST_SOURCE["overview"] = "coingecko"
            return data
    except Exception:  # noqa: BLE001
        pass

    rows = _hyper_overview(min(limit, len(COINS)))
    LAST_SOURCE["overview"] = "hyperliquid"
    return rows


def history(coin_id: str, days: int = 365) -> dict:
    """单个资产的历史价格与成交量。币安官方优先，失败逐级降级。"""
    symbol = _resolve_symbol(coin_id)
    if symbol:
        try:
            data = _binance_daily(symbol, days)
            LAST_SOURCE["history"] = "binance"
            return data
        except Exception:  # noqa: BLE001
            pass

    if coin_id in COIN_BY_ID:
        try:
            data = _get(f"/coins/{coin_id}/market_chart",
                        {"vs_currency": "usd", "days": days, "interval": "daily"})
            if isinstance(data, dict) and data.get("prices"):
                LAST_SOURCE["history"] = "coingecko"
                data["_source"] = "coingecko"
                return data
        except Exception:  # noqa: BLE001
            pass

    name = COIN_BY_ID.get(coin_id, (coin_id, coin_id.upper(), coin_id))[1]
    candles = _candles(name, days)
    prices = [[x.get("t", 0), float(x.get("c", 0))] for x in candles if x.get("c")]
    volumes = [[x.get("t", 0), float(x.get("v", 0))] for x in candles if x.get("v")]
    if not prices:
        raise RuntimeError("所有数据源都没有返回可用历史数据：" + str(coin_id))
    LAST_SOURCE["history"] = "hyperliquid"
    return {"prices": prices, "total_volumes": volumes, "_source": "hyperliquid"}
