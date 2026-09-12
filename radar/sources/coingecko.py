"""코인게코 무료 API — 트렌딩/테마(카테고리)/시총. 캐시로 레이트리밋 회피."""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import DATA_DIR
from ..http import get_json

BASE = "https://api.coingecko.com/api/v3"
_CACHE = DATA_DIR / "cg_cache.json"


def _cached(key: str, ttl: int, fetch) -> object:
    now = time.time()
    blob: dict = {}
    if _CACHE.exists():
        try:
            blob = json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            blob = {}
    ent = blob.get(key)
    if ent and now - ent.get("ts", 0) < ttl:
        return ent["v"]
    try:
        v = fetch()
    except Exception:  # noqa: BLE001
        return ent["v"] if ent else None
    blob[key] = {"ts": now, "v": v}
    try:
        _CACHE.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return v


def trending() -> list[dict]:
    d = _cached("trending", 900, lambda: get_json(f"{BASE}/search/trending", timeout=20))
    if not isinstance(d, dict):
        return []
    return [c.get("item", {}) for c in d.get("coins", [])]


def trending_symbols() -> set[str]:
    return {(c.get("symbol") or "").upper() for c in trending() if c.get("symbol")}


def markets(pages: int = 2) -> list[dict]:
    """시총 상위 코인 메타(심볼→이름/시총/카테고리 추정용)."""
    def fetch():
        rows = []
        for p in range(1, pages + 1):
            rows += get_json(f"{BASE}/coins/markets", timeout=25, params={
                "vs_currency": "usd", "order": "market_cap_desc",
                "per_page": 250, "page": p, "price_change_percentage": "1h,24h,7d"})
            time.sleep(1.2)
        return rows
    d = _cached(f"markets{pages}", 3600, fetch)
    return d if isinstance(d, list) else []


def symbol_meta() -> dict[str, dict]:
    """UPPER심볼 → {name, mcap, rank, chg7d}"""
    out: dict[str, dict] = {}
    for m in markets():
        sym = (m.get("symbol") or "").upper()
        if not sym or sym in out:
            continue
        out[sym] = {
            "name": m.get("name") or sym,
            "id": m.get("id"),
            "mcap": m.get("market_cap") or 0,
            "rank": m.get("market_cap_rank") or 9999,
            "chg7d": m.get("price_change_percentage_7d_in_currency") or 0,
            "chg1h": m.get("price_change_percentage_1h_in_currency") or 0,
        }
    return out


def hot_categories(top: int = 8) -> list[dict]:
    """24h 시총 변화 기준 뜨는 테마."""
    d = _cached("categories", 1800, lambda: get_json(f"{BASE}/coins/categories", timeout=30))
    if not isinstance(d, list):
        return []
    rows = [c for c in d if (c.get("market_cap") or 0) > 5e7]
    rows.sort(key=lambda c: c.get("market_cap_change_24h") or -999, reverse=True)
    return rows[:top]


# 급등 원인 판별에 자주 쓰는 테마들. 필요하면 여기 id만 추가하면 됨.
THEME_IDS = [
    ("meme-token", "밈코인"),
    ("artificial-intelligence", "AI"),
    ("real-world-assets-rwa", "RWA"),
    ("gaming", "게임"),
    ("depin", "DePIN"),
    ("solana-meme-coins", "솔라나 밈"),
    ("base-meme-coins", "베이스 밈"),
    ("layer-1", "L1"),
    ("layer-2", "L2"),
    ("decentralized-finance-defi", "DeFi"),
    ("privacy-coins", "프라이버시"),
]


def theme_map() -> dict[str, list[str]]:
    """UPPER심볼 → [테마 한글명]. 6시간 캐시."""
    def fetch():
        m: dict[str, list[str]] = {}
        ok = 0
        for cid, label in THEME_IDS:
            try:
                rows = get_json(f"{BASE}/coins/markets", timeout=25, params={
                    "vs_currency": "usd", "category": cid,
                    "order": "market_cap_desc", "per_page": 100, "page": 1})
            except Exception:  # noqa: BLE001
                time.sleep(3)
                continue
            ok += 1
            for r in rows:
                sym = (r.get("symbol") or "").upper()
                if sym:
                    m.setdefault(sym, [])
                    if label not in m[sym]:
                        m[sym].append(label)
            time.sleep(2.5)
        if ok < len(THEME_IDS) * 0.6:
            raise RuntimeError("theme_map 수집 부족 — 이전 캐시 유지")
        return m

    d = _cached("themes", 21600, fetch)
    return d if isinstance(d, dict) else {}
