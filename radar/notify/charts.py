"""무료·무키 차트 이미지 URL — quickchart.io (Chart.js 설정을 URL 에 담는다).

텔레그램 sendPhoto 에 URL 만 넘기면 텔레그램 서버가 받아간다.
점은 최대 48개, URL 은 2000자 안쪽으로 줄인다. 차트 안 글씨는 한글 폰트가 없을 수 있어 영문만.
"""
from __future__ import annotations

import json
from urllib.parse import quote

BASE = "https://quickchart.io/chart"
MAX_POINTS = 48
MAX_URL = 2000


def downsample(values: list[float], n: int = MAX_POINTS) -> list[float]:
    vals = [float(v) for v in values if v is not None]
    if len(vals) <= n:
        return vals
    step = (len(vals) - 1) / (n - 1)
    return [vals[round(i * step)] for i in range(n)]


def _round(v: float) -> float:
    return float(f"{v:.5g}")


def line_chart_url(values: list[float], title: str, first_label: str = "", last_label: str = "now",
                   color: str = "rgb(229,57,53)", w: int = 800, h: int = 400) -> str:
    """값 목록 → quickchart 선 차트 URL. 데이터가 2개 미만이면 빈 문자열."""
    n = MAX_POINTS
    while n >= 8:
        vals = [_round(v) for v in downsample(values, n)]
        if len(vals) < 2:
            return ""
        labels = [""] * len(vals)
        labels[0], labels[-1] = first_label, last_label
        cfg = {
            "type": "line",
            "data": {"labels": labels, "datasets": [{
                "data": vals, "fill": False, "borderColor": color, "borderWidth": 2,
                "pointRadius": 0, "lineTension": 0}]},
            "options": {"legend": {"display": False},
                        "title": {"display": True, "text": title[:60]}},
        }
        c = json.dumps(cfg, separators=(",", ":"), ensure_ascii=True)
        url = f"{BASE}?w={w}&h={h}&bkg=white&c={quote(c, safe=',:')}"
        if len(url) <= MAX_URL:
            return url
        n //= 2
    return ""


PALETTE = ["rgb(229,57,53)", "rgb(30,136,229)", "rgb(130,130,130)", "rgb(67,160,71)"]


def multi_line_chart_url(series: dict[str, list[float]], title: str, w: int = 800, h: int = 400) -> str:
    """여러 선(예: 대장주 vs 2·3등 장중 등락률) 비교 차트. 모든 선을 같은 점 개수로 맞춘다.
    색: 첫 번째 빨강, 두 번째 파랑, 세 번째 회색."""
    items = [(k, [float(x) for x in v if x is not None]) for k, v in series.items()]
    items = [(k, v) for k, v in items if len(v) >= 2]
    if not items:
        return ""
    n = min(MAX_POINTS, min(len(v) for _, v in items))
    while n >= 8 or (n >= 2 and n == min(len(v) for _, v in items)):
        data = [(k, [round(x, 2) for x in downsample(v, n)]) for k, v in items]
        labels = [""] * n
        labels[0], labels[-1] = "open", "now"
        cfg = {
            "type": "line",
            "data": {"labels": labels, "datasets": [
                {"label": k[:24], "data": d, "fill": False, "borderColor": PALETTE[i % len(PALETTE)],
                 "borderWidth": 3 if i == 0 else 2, "pointRadius": 0, "lineTension": 0}
                for i, (k, d) in enumerate(data)]},
            "options": {"legend": {"display": True}, "title": {"display": True, "text": title[:60]}},
        }
        c = json.dumps(cfg, separators=(",", ":"), ensure_ascii=True)
        url = f"{BASE}?w={w}&h={h}&bkg=white&c={quote(c, safe=',:')}"
        if len(url) <= MAX_URL:
            return url
        if n <= 8:
            break
        n //= 2
    return ""
