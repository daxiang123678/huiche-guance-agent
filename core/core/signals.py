"""Business calculations for drawdown and volume-profile zones."""
from __future__ import annotations


def drawdown_rows(markets: list[dict]) -> list[dict]:
    rows = []
    for item in markets:
        price = float(item.get("current_price") or 0)
        ath = float(item.get("ath") or 0)
        drawdown = (price / ath - 1) * 100 if ath else 0
        rows.append({
            "id": item.get("id"), "symbol": str(item.get("symbol", "")).upper(),
            "name": item.get("name"), "image": item.get("image"), "price": price,
            "ath": ath, "ath_date": item.get("ath_date", {}).get("usd") if isinstance(item.get("ath_date"), dict) else None,
            "drawdown": round(drawdown, 2), "market_cap": item.get("market_cap"),
            "change_24h": item.get("price_change_percentage_24h"),
        })
    return sorted(rows, key=lambda x: x["drawdown"])


def zones(history: dict, bins: int = 18) -> dict:
    prices = [float(row[1]) for row in history.get("prices", []) if len(row) > 1 and row[1]]
    volumes = [float(row[1]) for row in history.get("total_volumes", []) if len(row) > 1 and row[1]]
    if len(prices) < 8:
        return {"prices": prices, "volumes": volumes, "profile": [], "support": [], "resistance": [], "current": prices[-1] if prices else 0}
    low, high = min(prices), max(prices)
    width = (high - low) / bins or 1
    profile = []
    for index in range(bins):
        left, right = low + index * width, low + (index + 1) * width
        volume = sum(volumes[i] for i, price in enumerate(prices[:len(volumes)]) if left <= price < right)
        profile.append({"price": round((left + right) / 2, 6), "volume": round(volume, 2)})
    peak = max(profile, key=lambda x: x["volume"])
    current = prices[-1]
    supports = sorted([p["price"] for p in profile if p["price"] <= current], key=lambda p: abs(current-p),)[:3]
    resistances = sorted([p["price"] for p in profile if p["price"] > current], key=lambda p: abs(current-p))[:3]
    return {"prices": prices, "volumes": volumes, "profile": profile, "support": supports, "resistance": resistances, "current": current, "peak_zone": peak["price"]}
