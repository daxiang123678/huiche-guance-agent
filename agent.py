#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高位回撤观测智能体 —— 命令行入口（四通道）

本文件让「高位回撤观测智能体」除了网页形态之外，还能被当成一个技能（Skill）直接调用。
数据全部来自币安官方公开资源，计算全部由本项目自研引擎（core/signals.py）完成。

四个通道
--------
    --live       币安官方公开行情接口（data-api.binance.vision）—— 默认通道
    --skill      币安官方技能 + 官方 CLI（binance-cli 驱动 is 同一个官方公开入口）
    --official   币安官方开源数据仓库（data.binance.vision 的历史 ZIP 归档）
    --global     综合平台全市场口径（非官方，仅用于对照）

用法示例
--------
    python agent.py                              # 默认通道，回撤榜前 20 个币
    python agent.py --live --top 30
    python agent.py --skill --json
    python agent.py --official --months 24 --top 12
    python agent.py --symbols BTC,ETH,SOL --depth 3
    python agent.py "看看哪些币回撤得最狠"          # 自然语言，自动选通道
    python agent.py "用官方开源数据看看 BTC"        # 自然语言 → --official

约定
----
* 加了 --json 时，stdout 只输出纯 JSON，所有进度与说明都走 stderr。
* 不给任何通道参数时默认走 --live；自然语言只在「没显式给通道」时才用来选通道。
* 绝不伪造数据：取不到的字段如实留空或标 0，并在 honesty 字段里说明。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.signals import drawdown_rows, zones  # noqa: E402  自研引擎，本文件全程复用

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DATA_API = "https://data-api.binance.vision"      # 币安官方公开行情接口
DATA_REPO = "https://data.binance.vision"         # 币安官方开源数据仓库
USER_AGENT = "huiche-guance-agent/1.0"
TIMEOUT = 20
RETRIES = 3
DEFAULT_TOP = 20
DEFAULT_DEPTH = 3
DEFAULT_MONTHS = 24
CONCURRENCY = 8

# 稳定币不参与「回撤」排行（它们本来就不该远离锚定价）
STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "AEUR", "USD1",
    "XUSD", "EUR", "EURI", "TRY", "BRL", "ARS", "JPY", "ZAR", "GBP", "USDE", "USDY",
}
# 杠杆代币后缀（BTCUP / ETHDOWN 这类），不参与排行
LEVERAGED_SUFFIX = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

# 直连官方数据域。显式忽略系统代理 —— 本机测试发现系统代理开关常与代理软件实际状态
# 不一致（开关开着但代理进程没跑），会导致所有请求失败，而这两个官方数据域直连是通的。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def say(msg: str, quiet: bool = False) -> None:
    """进度输出统一走 stderr，保证 stdout 在 JSON 模式下是干净的。"""
    if not quiet:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def http_get_raw(url: str, timeout: int = TIMEOUT, retries: int = RETRIES) -> bytes:
    """带重试的 GET，返回原始字节。失败如实抛出，不吞异常。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with OPENER.open(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(0.4 * (attempt + 1))
    raise last if last else RuntimeError("请求失败：" + url)


def http_get_json(url: str, timeout: int = TIMEOUT, retries: int = RETRIES):
    return json.loads(http_get_raw(url, timeout, retries).decode("utf-8"))


def norm_ts(value) -> int:
    """把时间戳统一成毫秒。

    实测坑：官方历史 CSV 在不同年份用了不同精度（早期文件是毫秒 13 位，
    较新的文件出现过微秒 16 位），所以这里按量级自适应，不能写死。
    """
    x = int(float(value))
    while x > 10 ** 14:
        x //= 1000
    return x


def ts_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def ts_month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def recent_months(count: int) -> list[str]:
    """返回最近 count 个「已完整结束」的月份，格式 YYYY-MM，从近到远。"""
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month
    out: list[str] = []
    for _ in range(count):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        out.append(f"{year:04d}-{month:02d}")
    return out


def is_leveraged(base: str) -> bool:
    """判断是不是杠杆代币。

    带长度保护：剥离后缀后只剩 1 个字符的不算，否则会把 JUP 这类正常币误杀。
    """
    for suffix in LEVERAGED_SUFFIX:
        if base.endswith(suffix):
            head = base[: -len(suffix)]
            if len(head) >= 2:
                return True
    return False


def to_binance_symbol(text: str) -> str:
    """把 BTC / btc / BTCUSDT 统一成 BTCUSDT。"""
    s = text.strip().upper().replace("_", "").replace("-", "")
    if not s:
        return s
    if s.endswith("USDT"):
        return s
    return s + "USDT"


def base_of(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


# --------------------------------------------------------------------------
# 币安官方公开行情接口（data-api.binance.vision）
# --------------------------------------------------------------------------

def ticker_all() -> list[dict]:
    """全市场 24h 快照，一次请求拿全，不需要逐币查。"""
    data = http_get_json(DATA_API + "/api/v3/ticker/24hr", timeout=40)
    return data if isinstance(data, list) else []


def klines(symbol: str, interval: str, limit: int) -> list[list]:
    """官方 K 线。

    返回每行 12 列：
    0 开盘时间 1 开 2 高 3 低 4 收 5 成交量 6 收盘时间
    7 成交额(计价币) 8 成交笔数 9 主动买量 10 主动买额 11 忽略
    """
    # 官方确实存在带非 ASCII 字符的交易对（实测碰到过中文符号的币），
    # 所以 symbol 一定要做 URL 编码，否则请求直接报错。
    safe_symbol = quote(symbol, safe="")
    url = (DATA_API + f"/api/v3/klines?symbol={safe_symbol}"
           f"&interval={interval}&limit={limit}")
    data = http_get_json(url, timeout=25)
    return data if isinstance(data, list) else []


def pick_universe(top: int, symbols: str | None) -> list[dict]:
    """挑出要观测的币种。

    默认按 24h 成交额从高到低取 USDT 交易对，动态生成币种宇宙，
    不写死名单（币安上新能自动跟上）。也可以用 --symbols 显式指定。
    """
    if symbols:
        wanted = [to_binance_symbol(x) for x in symbols.split(",") if x.strip()]
        rows = ticker_all()
        by_symbol = {r.get("symbol"): r for r in rows}
        out: list[dict] = []
        for sym in wanted:
            row = by_symbol.get(sym)
            if row:
                out.append(row)
            else:
                say(f"  [提示] 官方接口里找不到交易对 {sym}，已跳过")
        return out

    rows = ticker_all()
    picked: list[dict] = []
    for row in rows:
        sym = str(row.get("symbol", ""))
        if not sym.endswith("USDT"):
            continue
        base = base_of(sym)
        if base in STABLE_BASES or is_leveraged(base):
            continue
        if base.isdigit():          # 过滤纯数字代码
            continue
        picked.append(row)
    picked.sort(key=lambda r: float(r.get("quoteVolume") or 0), reverse=True)
    return picked[:top]


def binance_ath(symbol: str) -> dict:
    """用官方月线算真实历史最高价。

    月线的「最高价」列就是该月最高价，取所有月份的最大值即为全历史 ATH。
    limit=1000 足够覆盖任何币种的上市至今（币安月线最长约 110 个月）。
    """
    rows = klines(symbol, "1M", 1000)
    if not rows:
        return {}
    best_high = -1.0
    best_ts = 0
    first_ts = 0
    for r in rows:
        try:
            high = float(r[2])
            ts = norm_ts(r[0])
        except (IndexError, TypeError, ValueError):
            continue
        if first_ts == 0:
            first_ts = ts
        if high > best_high:
            best_high, best_ts = high, ts
    if best_high <= 0:
        return {}
    return {
        "ath": best_high,
        "athMonth": ts_month(best_ts),
        "listedMonth": ts_month(first_ts) if first_ts else None,
        "monthsCovered": len(rows),
    }


def binance_daily_history(symbol: str, days: int = 365) -> dict:
    """取日线并转成自研引擎认得的形状。

    引擎要的是 {prices: [[时间, 价]], total_volumes: [[时间, 量]]}，
    这里用「收盘价 + 成交额」填充，口径与引擎原有实现保持一致。
    """
    rows = klines(symbol, "1d", days)
    prices: list[list] = []
    volumes: list[list] = []
    for r in rows:
        try:
            ts = norm_ts(r[0])
            prices.append([ts, float(r[4])])       # 收盘价
            volumes.append([ts, float(r[7])])      # 成交额（计价币）
        except (IndexError, TypeError, ValueError):
            continue
    return {"prices": prices, "total_volumes": volumes}


# --------------------------------------------------------------------------
# 币安官方技能 + 官方 CLI（binance-cli）
# --------------------------------------------------------------------------

def binance_cli_bin() -> str | None:
    """定位官方 CLI：优先环境变量 BINANCE_CLI_PATH，其次从 PATH 里找。"""
    env_path = os.environ.get("BINANCE_CLI_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    for name in ("binance-cli", "binance-cli.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def cli_json(path: str):
    """用官方 CLI 的 request 子命令调官方公开入口。

    这是币安官方 binance 技能文档里记载的标准用法
    （SKILL.md 末行：For endpoints not listed in the skill, use
    `binance-cli request (GET|POST|...) <url>`）。
    """
    exe = binance_cli_bin()
    if not exe:
        raise RuntimeError("没有找到官方 CLI")
    url = path if path.startswith("http") else DATA_API + path
    proc = subprocess.run(
        [exe, "request", "GET", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"官方 CLI 返回码 {proc.returncode}：{(proc.stderr or '').strip()[:200]}")
    text = (proc.stdout or "").strip()
    if not text:
        raise RuntimeError("官方 CLI 没有输出内容")
    return json.loads(text)


# --------------------------------------------------------------------------
# 币安官方开源数据仓库（data.binance.vision 历史 ZIP）
# --------------------------------------------------------------------------

def repo_month_daily(symbol: str, ym: str) -> list[list]:
    """下载官方仓库里某个月的日线 ZIP 并解析。

    实测：spot 的月度文件内部是纯 CSV，**没有表头**，12 列，每月一个 csv。
    """
    # 官方确实存在带非 ASCII 字符的交易对（实测碰到过中文符号的币），
    # 归档路径同样要做 URL 编码，否则请求会因为编码错误直接失败。
    safe = quote(symbol, safe="")
    url = f"{DATA_REPO}/data/spot/monthly/klines/{safe}/1d/{safe}-1d-{ym}.zip"
    try:
        raw = http_get_raw(url, timeout=25, retries=2)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []          # 该月还没有发布（官方仓库通常是 T+1），如实跳过
        raise
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        member = zf.namelist()[0]
        text = zf.read(member).decode("utf-8", errors="replace")
    rows: list[list] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("open"):
            continue
        parts = line.split(",")
        if len(parts) >= 8:
            rows.append(parts)
    return rows


def repo_history(symbol: str, months: int, quiet: bool) -> tuple[list[list], dict]:
    """并发拉取最近 N 个月的官方日线文件。"""
    want = recent_months(months)
    all_rows: list[list] = []
    hit, miss = 0, 0

    def one(ym: str):
        return ym, repo_month_daily(symbol, ym)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for ym, rows in pool.map(one, want):
            if rows:
                hit += 1
                all_rows.extend(rows)
            else:
                miss += 1

    all_rows.sort(key=lambda r: norm_ts(r[0]))
    meta = {"monthsRequested": months, "monthsFound": hit, "monthsMissing": miss}
    say(f"    官方仓库 {symbol}：请求 {months} 个月，命中 {hit}，缺失 {miss}")
    return all_rows, meta


def rows_to_history(rows: list[list]) -> dict:
    prices: list[list] = []
    volumes: list[list] = []
    for r in rows:
        try:
            ts = norm_ts(r[0])
            prices.append([ts, float(r[4])])
            volumes.append([ts, float(r[7])])
        except (IndexError, TypeError, ValueError):
            continue
    return {"prices": prices, "total_volumes": volumes}


# --------------------------------------------------------------------------
# 通道 1：官方公开行情接口（默认）
# --------------------------------------------------------------------------

def collect_live(top: int, depth: int, symbols: str | None, quiet: bool) -> dict:
    say("通道：--live　币安官方公开行情接口（data-api.binance.vision）")
    universe = pick_universe(top, symbols)
    if not universe:
        raise RuntimeError("官方接口没有返回可用交易对")
    say(f"  币种宇宙：{len(universe)} 个 USDT 交易对")

    def ath_of(row):
        sym = str(row.get("symbol"))
        try:
            return sym, binance_ath(sym)
        except Exception as exc:  # noqa: BLE001
            say(f"  [降级] {sym} 的历史高点取失败：{exc}")
            return sym, {}

    say("  正在拉取各币全历史月线以计算真实 ATH ...")
    ath_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for sym, info in pool.map(ath_of, universe):
            ath_map[sym] = info

    overview = build_overview(universe, ath_map, "币安官方公开行情接口 · 全历史月线")
    zones_map, zone_meta = build_zones(overview, depth, quiet, source="live")
    return {
        "source": "live",
        "sourceLabel": "币安官方公开行情接口",
        "sourceDetail": {
            "endpoints": [
                DATA_API + "/api/v3/ticker/24hr",
                DATA_API + "/api/v3/klines?interval=1M",
                DATA_API + "/api/v3/klines?interval=1d",
            ],
            "auth": "免密钥",
        },
        "overview": overview,
        "zones": zones_map,
        "zoneMeta": zone_meta,
        "honesty": [
            "ATH 为币安现货口径（基于官方月线最高价），与全市场综合口径可能略有差异。",
            "现货快照不含合约持仓量，本作品不使用持仓量指标。",
        ],
    }


# --------------------------------------------------------------------------
# 通道 2：官方技能 + 官方 CLI
# --------------------------------------------------------------------------

def collect_skill(top: int, depth: int, symbols: str | None, quiet: bool) -> dict:
    say("通道：--skill　币安官方技能（binance）+ 官方 CLI（binance-cli）")
    exe = binance_cli_bin()
    if not exe:
        say("  [如实回退] 本机没有找到官方 CLI，已改走 --live 通道。")
        say("  想装官方 CLI，去币安官方仓库 binance/binance-cli 下载 v2.0.0 的 Windows 版。")
        payload = collect_live(top, depth, symbols, quiet)
        payload["source"] = "live"
        payload["requestedSource"] = "skill"
        payload["sourceLabel"] = "币安官方公开行情接口（官方 CLI 未安装，已如实回退）"
        payload["honesty"] = list(payload.get("honesty", [])) + [
            "本机没有安装官方 CLI，因此这一次实际走的是公开行情接口，不是 CLI 通道。",
        ]
        return payload

    say(f"  官方 CLI：{exe}")

    # 一次拿全市场快照（走官方 CLI 的 request 子命令）
    rows = cli_json("/api/v3/ticker/24hr")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("官方 CLI 没有返回行情数据")
    say(f"  官方 CLI 返回 {len(rows)} 个交易对")

    if symbols:
        wanted = {to_binance_symbol(x) for x in symbols.split(",") if x.strip()}
        universe = [r for r in rows if r.get("symbol") in wanted]
    else:
        universe = []
        for row in rows:
            sym = str(row.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            base = base_of(sym)
            if base in STABLE_BASES or is_leveraged(base) or base.isdigit():
                continue
            universe.append(row)
        universe.sort(key=lambda r: float(r.get("quoteVolume") or 0), reverse=True)
        universe = universe[:top]
    if not universe:
        raise RuntimeError("官方 CLI 通道没有筛出可用交易对")

    def ath_of(row):
        sym = str(row.get("symbol"))
        try:
            data = cli_json(f"/api/v3/klines?symbol={sym}&interval=1M&limit=1000")
        except Exception as exc:  # noqa: BLE001
            say(f"  [降级] {sym} 历史高点取失败：{exc}")
            return sym, {}
        best_high, best_ts, first_ts = -1.0, 0, 0
        for r in data if isinstance(data, list) else []:
            try:
                high, ts = float(r[2]), norm_ts(r[0])
            except (IndexError, TypeError, ValueError):
                continue
            if first_ts == 0:
                first_ts = ts
            if high > best_high:
                best_high, best_ts = high, ts
        if best_high <= 0:
            return sym, {}
        return sym, {
            "ath": best_high,
            "athMonth": ts_month(best_ts),
            "listedMonth": ts_month(first_ts) if first_ts else None,
        }

    say("  正在通过官方 CLI 拉取各币全历史月线 ...")
    ath_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for sym, info in pool.map(ath_of, universe):
            ath_map[sym] = info

    overview = build_overview(universe, ath_map, "币安官方技能(binance)驱动官方 CLI · 全历史月线")
    zones_map, zone_meta = build_zones(overview, depth, quiet, source="skill",
                                       fetcher=lambda sym, days: _cli_daily(sym, days))
    return {
        "source": "skill",
        "sourceLabel": "币安官方技能 binance + 官方 CLI binance-cli",
        "sourceDetail": {
            "cliPath": exe,
            "command": "binance-cli request GET <币安官方公开入口>",
            "note": "调用方式取自官方 binance 技能 SKILL.md 记载的 request 子命令",
        },
        "overview": overview,
        "zones": zones_map,
        "zoneMeta": zone_meta,
        "honesty": [
            "ATH 为币安现货口径（基于官方月线最高价）。",
            "官方 CLI 的 request 子命令是官方技能文档记载的标准用法，不是绕路。",
        ],
    }


def _cli_daily(symbol: str, days: int) -> dict:
    data = cli_json(f"/api/v3/klines?symbol={symbol}&interval=1d&limit={days}")
    prices, volumes = [], []
    for r in data if isinstance(data, list) else []:
        try:
            ts = norm_ts(r[0])
            prices.append([ts, float(r[4])])
            volumes.append([ts, float(r[7])])
        except (IndexError, TypeError, ValueError):
            continue
    return {"prices": prices, "total_volumes": volumes}


# --------------------------------------------------------------------------
# 通道 3：官方开源数据仓库
# --------------------------------------------------------------------------

def collect_official(top: int, depth: int, symbols: str | None, quiet: bool,
                     months: int) -> dict:
    say(f"通道：--official　币安官方开源数据仓库（data.binance.vision，回溯 {months} 个月）")
    universe = pick_universe(top, symbols)
    if not universe:
        raise RuntimeError("官方接口没有返回可用交易对")
    say(f"  币种宇宙：{len(universe)} 个 USDT 交易对（冻结在最近一个月内，官方仓库为 T+1 归档）")

    def work(row):
        sym = str(row.get("symbol"))
        rows, meta = repo_history(sym, months, quiet)
        return sym, rows, meta

    result: dict[str, tuple[list[list], dict]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for sym, rows, meta in pool.map(work, universe):
            result[sym] = (rows, meta)

    overview = []
    for row in universe:
        sym = str(row.get("symbol"))
        rows, meta = result.get(sym, ([], {}))
        price = float(row.get("lastPrice") or 0)
        # 在窗口内正经找最高价和它的月份，不能只看最后一行
        window_high, window_low, high_ts = 0.0, 0.0, 0
        for r in rows:
            try:
                high, low, ts = float(r[2]), float(r[3]), norm_ts(r[0])
            except (IndexError, TypeError, ValueError):
                continue
            if window_low == 0.0 or low < window_low:
                window_low = low
            if high > window_high:
                window_high, high_ts = high, ts
        base = base_of(sym)
        overview.append({
            "symbol": sym, "base": base, "name": base,
            "price": price,
            "ath": window_high,
            "athScope": f"近 {months} 个月窗口内最高价（官方仓库口径）",
            "athMonth": ts_month(high_ts) if high_ts else None,
            "windowLow": window_low,
            "drawdown": round((price / window_high - 1) * 100, 2) if window_high else None,
            "change_24h": float(row.get("priceChangePercent") or 0),
            "quoteVolume": float(row.get("quoteVolume") or 0),
            "daysCovered": len(rows),
            "monthsFound": meta.get("monthsFound"),
            "monthsMissing": meta.get("monthsMissing"),
        })
    # 拿不到数据的币排到最后，不参与名次，也不假装是 0
    overview.sort(key=lambda x: (x["drawdown"] if x["drawdown"] is not None else float("inf")))

    zone_meta: dict = {"scope": f"近 {months} 个月官方仓库日线"}
    zones_map: dict = {}
    picked = overview[:depth] if depth > 0 else []
    for item in picked:
        sym = item["symbol"]
        rows, _ = result.get(sym, ([], []))
        if rows:
            zones_map[sym] = zones(rows_to_history(rows))
    if picked:
        say(f"  已对回撤最深的前 {len(zones_map)} 个币完成成交量分布分析")

    return {
        "source": "official",
        "sourceLabel": "币安官方开源数据仓库",
        "sourceDetail": {
            "repo": DATA_REPO,
            "path": "data/spot/monthly/klines/<交易对>/1d/<交易对>-1d-YYYY-MM.zip",
            "format": "官方月度日线归档，CSV，12 列，无表头",
            "months": months,
            "universe": "币种名单取自币安官方公开行情接口快照，不写死清单，上新能自动跟上",
        },
        "overview": overview,
        "zones": zones_map,
        "zoneMeta": zone_meta,
        "honesty": [
            f"本通道是官方仓库归档口径，ATH 只覆盖近 {months} 个月；要全历史 ATH 请用 --live 或 --skill。",
            "本通道的百分比是相对「回溯窗口内最高价」算出来的，现价高于窗口最高价时会出现正数，"
            "属于正常现象，不是计算错误。",
            "官方仓库为 T+1 归档，最新一两天通常还没发布，缺失的月份已在 monthsMissing 里如实标出。",
            "实时价格仍来自公开行情接口，官方仓库只提供历史归档。",
        ],
    }


# --------------------------------------------------------------------------
# 通道 4：综合平台全市场口径（非官方，对照用）
# --------------------------------------------------------------------------

def collect_global(top: int, depth: int, symbols: str | None, quiet: bool) -> dict:
    say("通道：--global　综合平台全市场口径（非官方，仅用于对照）")
    from core import market_data as md  # 延迟导入，避免非本通道时也依赖

    markets = md.market_overview(limit=max(top, 20))
    if not isinstance(markets, list) or not markets:
        raise RuntimeError("综合平台数据源没有返回数据")
    overview = []
    for item in markets[:top]:
        price = float(item.get("current_price") or 0)
        ath = float(item.get("ath") or 0)
        overview.append({
            "symbol": str(item.get("symbol", "")).upper(),
            "base": str(item.get("symbol", "")).upper(),
            "name": item.get("name"),
            "price": price,
            "ath": ath,
            "athScope": "全市场综合口径",
            "drawdown": round((price / ath - 1) * 100, 2) if ath else None,
            "change_24h": item.get("price_change_percentage_24h"),
            "quoteVolume": None,
        })
    # 拿不到数据的币排到最后，不参与名次，也不假装是 0
    overview.sort(key=lambda x: (x["drawdown"] if x["drawdown"] is not None else float("inf")))

    zones_map: dict = {}
    for item in markets[:depth]:
        try:
            zones_map[str(item.get("symbol", "")).upper()] = zones(md.history(item.get("id")))
        except Exception as exc:  # noqa: BLE001
            say(f"  [降级] {item.get('symbol')} 成交分布分析失败：{exc}")

    return {
        "source": "global",
        "sourceLabel": "综合平台全市场口径（非币安官方）",
        "sourceDetail": {"note": "此通道用于对照，不代表官方口径"},
        "overview": overview,
        "zones": zones_map,
        "zoneMeta": {"scope": "近一年日线（综合平台口径）"},
        "honesty": [
            "本通道使用的是综合平台数据，不是币安官方资源；列在这里只为对照官方口径的差异。",
        ],
    }


# --------------------------------------------------------------------------
# 公共装配
# --------------------------------------------------------------------------

def build_overview(universe: list[dict], ath_map: dict, ath_scope: str) -> list[dict]:
    """把官方快照 + ATH 装成自研引擎认得的形状，再交给引擎排序。"""
    markets = []
    for row in universe:
        sym = str(row.get("symbol"))
        info = ath_map.get(sym, {})
        markets.append({
            "id": base_of(sym).lower(),
            "symbol": base_of(sym),
            "name": base_of(sym),
            "image": "",
            "current_price": float(row.get("lastPrice") or 0),
            "ath": float(info.get("ath") or 0),
            "ath_date": {"usd": info.get("athMonth")},
            "market_cap": float(row.get("quoteVolume") or 0),
            "price_change_percentage_24h": float(row.get("priceChangePercent") or 0),
        })

    rows = drawdown_rows(markets)          # ← 自研引擎，排序与回撤计算都在这里
    detail = {str(r.get("symbol")): r for r in universe}
    for row in rows:
        sym = row["symbol"] + "USDT"
        src = detail.get(sym, {})
        info = ath_map.get(sym, {})
        row["symbol"] = sym                  # 输出统一用币安全名，便于核对
        row["base"] = base_of(sym)
        row["name"] = base_of(sym)
        row["price"] = row.pop("price")
        row["quoteVolume"] = float(src.get("quoteVolume") or 0)
        # 币安现货快照里没有「市值」这个字段，如实删掉，
        # 不能把成交额换个名字冒充市值。
        row.pop("market_cap", None)
        row["athMonth"] = info.get("athMonth")
        row["athScope"] = ath_scope
        row["listedMonth"] = info.get("listedMonth")
    return rows


def build_zones(overview: list[dict], depth: int, quiet: bool, source: str,
                fetcher=None) -> tuple[dict, dict]:
    """对回撤最深的前 N 个币做成交量分布与支撑压力分析。"""
    zones_map: dict = {}
    picked = overview[:depth] if depth > 0 else []
    if not picked:
        return zones_map, {"analyzed": 0}
    say(f"  正在对回撤最深的前 {len(picked)} 个币做成交分布分析 ...")
    getter = fetcher or (lambda sym, days: binance_daily_history(sym, days))
    for item in picked:
        sym = item["symbol"]
        try:
            zones_map[sym] = zones(getter(sym, 365))
        except Exception as exc:  # noqa: BLE001
            say(f"  [降级] {sym} 成交分布分析失败：{exc}")
    return zones_map, {"analyzed": len(zones_map), "window": "近 365 根日线"}


def summarize(overview: list[dict]) -> dict:
    """汇总统计。

    口径提醒：--official 用的是「窗口内最高价」，现价有可能高于窗口最高价，
    这种情况下 drawdown 是正整数（表示已经创出窗口新高）。所以这里用
    「最深 / 最浅」而不是「最大 / 最小」，避免正负号带来的误读。
    """
    values = [x["drawdown"] for x in overview if isinstance(x.get("drawdown"), (int, float))]
    if not values:
        return {"count": len(overview)}
    return {
        "count": len(overview),
        "deepestDrawdown": round(min(values), 2),
        "avgDrawdown": round(sum(values) / len(values), 2),
        "deepCount": sum(1 for v in values if v <= -70),
        "shallowestDrawdown": round(max(values), 2),
        "noDataCount": sum(1 for x in overview if x.get("drawdown") is None),
    }


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def channel_from_intent(text: str) -> str | None:
    """把一句自然语言映射到数据通道。

    只做「关键词 → 通道」的保守映射：识别不出来就返回 None，沿用默认通道。
    绝不根据一句话去猜币种或改参数 —— 猜错比不猜更糟，宁可什么都不动。
    """
    if any(k in text for k in ("官方技能", "技能包", "binance-cli", "binance cli")):
        return "skill"
    if any(k in text for k in ("官方开源", "开源数据", "官方数据仓库", "公开数据仓库", "历史数据", "官方历史")):
        return "official"
    if any(k in text for k in ("官方公开行情", "官方行情", "官方接口", "官方实时", "行情接口")):
        return "live"
    if any(k in text for k in ("综合平台", "多平台", "对照", "全市场口径")):
        return "global"
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="高位回撤观测智能体 —— 命令行入口（四通道）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python agent.py\n"
               "  python agent.py --skill --json\n"
               "  python agent.py --official --months 24 --top 12\n"
               "  python agent.py --symbols BTC,ETH,SOL --depth 3\n"
               "  python agent.py \"看看哪些币回撤得最狠\"\n"
               "  python agent.py \"用官方开源数据看看 BTC 距最高还有多远\"\n",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="币安官方公开行情接口（默认）")
    g.add_argument("--skill", action="store_true", help="币安官方技能 + 官方 CLI")
    g.add_argument("--official", action="store_true", help="币安官方开源数据仓库")
    g.add_argument("--global", dest="global_src", action="store_true",
                   help="综合平台全市场口径（非官方，对照用）")
    p.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"回撤榜取前几个币（默认 {DEFAULT_TOP}）")
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH,
                   help=f"对回撤最深的前几个币做成交分布分析（默认 {DEFAULT_DEPTH}，0 表示不做）")
    p.add_argument("--symbols", default=None, help="指定币种，逗号分隔，如 BTC,ETH,SOL")
    p.add_argument("--months", type=int, default=DEFAULT_MONTHS,
                   help=f"--official 通道回溯月数（默认 {DEFAULT_MONTHS}）")
    p.add_argument("--json", action="store_true", help="stdout 只输出纯 JSON")
    p.add_argument("--proxy", default=None, help="走指定代理，如 http://127.0.0.1:7890；默认直连")
    p.add_argument("--concurrency", type=int, default=CONCURRENCY,
                   help=f"逐币下载的并发数（默认 {CONCURRENCY}，上限 32）")
    p.add_argument("intent", nargs="*",
                   help="自然语言意图（可省略），如 \"看看哪些币回撤得最狠\"；"
                        "识别到「官方技能 / 官方开源 / 综合平台」会自动切换通道")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = args.json

    global OPENER, CONCURRENCY
    if args.proxy:
        OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": args.proxy, "https": args.proxy}))
        say(f"已启用代理：{args.proxy}")

    CONCURRENCY = max(1, min(int(args.concurrency or CONCURRENCY), 32))

    # 自然语言入口：只在用户没显式指定通道时生效 —— 显式参数永远优先。
    intent = " ".join(args.intent).strip()
    if intent:
        explicit = args.live or args.skill or args.official or args.global_src
        if explicit:
            say(f"已显式指定数据通道，忽略自然语言里的通道意图（原句：{intent}）", quiet)
        else:
            guessed = channel_from_intent(intent)
            if guessed:
                args.skill = guessed == "skill"
                args.official = guessed == "official"
                args.global_src = guessed == "global"
                say(f"已按自然语言意图选择通道：{guessed}（原句：{intent}）", quiet)
            else:
                say(f"未从自然语言中识别出数据通道，沿用默认通道（原句：{intent}）", quiet)

    started = time.time()
    try:
        if args.skill:
            payload = collect_skill(args.top, args.depth, args.symbols, quiet)
        elif args.official:
            payload = collect_official(args.top, args.depth, args.symbols, quiet, args.months)
        elif args.global_src:
            payload = collect_global(args.top, args.depth, args.symbols, quiet)
        else:
            payload = collect_live(args.top, args.depth, args.symbols, quiet)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        say("执行失败：" + message)
        if args.json:
            sys.stdout.write(json.dumps(
                {"task": "高位回撤观测", "ok": False, "error": message},
                ensure_ascii=False, indent=2))
            sys.stdout.write("\n")
        return 1

    payload["task"] = "高位回撤观测"
    payload["ok"] = True
    payload["generatedAt"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    payload["elapsedSeconds"] = round(time.time() - started, 2)
    payload["summary"] = summarize(payload.get("overview", []))
    payload["engine"] = "core/signals.py（自研）"

    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0

    # 人类可读输出
    print("=" * 72)
    print(" 高位回撤观测智能体")
    print("=" * 72)
    print(f" 数据来源：{payload['sourceLabel']}")
    print(f" 通道标识：{payload['source']}      耗时 {payload['elapsedSeconds']} 秒")
    s = payload["summary"]
    print(f" 观测币种：{s.get('count')} 个    最深回撤 {s.get('deepestDrawdown')}%    "
          f"平均回撤 {s.get('avgDrawdown')}%    深度回撤 {s.get('deepCount')} 个    "
          f"无数据 {s.get('noDataCount')} 个")
    print("-" * 72)
    print(f" {'币种':<12}{'现价':>14}{'历史最高':>14}{'距最高':>10}{'24h':>9}")
    print("-" * 72)
    # 取值一律走 .get()：这些字段由「四个通道 + 自研引擎」共同产出，
    # 任何一个通道漏给字段，都不该让「打印表格」这一步把整个程序崩掉。
    # （历史 bug：这里曾写死 row['change24h']，而实际字段名是 change_24h → KeyError）
    for row in payload["overview"]:
        price = row.get("price")
        ath = row.get("ath")
        dd = row.get("drawdown")
        chg = row.get("change_24h")
        print(f" {str(row.get('symbol', '?')):<12}"
              f"{(price if price is not None else 0):>14,.4f}"
              f"{(ath if ath is not None else 0):>14,.4f}"
              f"{(dd if dd is not None else 0):>9.2f}%"
              f"{(chg if chg is not None else 0):>8.2f}%")
    for sym, z in (payload.get("zones") or {}).items():
        sup = [v for v in (z.get("support") or [])[:2] if v is not None]
        res = [v for v in (z.get("resistance") or [])[:2] if v is not None]
        support = " / ".join(f"{v:,.4f}" for v in sup) or "暂无"
        resist = " / ".join(f"{v:,.4f}" for v in res) or "暂无"
        current = z.get("current")
        print("-" * 72)
        print(f" {sym} 当前价 {(current if current is not None else 0):,.4f}")
        print(f"   下方支撑：{support}")
        print(f"   上方压力：{resist}")
    print("=" * 72)
    for line in payload.get("honesty", []):
        print(" · " + line)
    print("\n分析结果仅用于研究，不构成投资建议。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
