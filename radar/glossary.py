"""어려운 용어 풀이 — 슬랙·텔레그램 공용.

메시지 한 건 안에서 용어가 **처음 나올 때만** "용어(풀이)" 로 한 번 풀어 준다.
링크(<url|라벨>)·코드(`...`) 안은 건드리지 않고, 굵게(*...*) 안에서 나오면 굵은 글씨 뒤에 붙인다.
"""
from __future__ import annotations

import re

TERMS: dict[str, str] = {
    "펀딩비": "선물 투자자끼리 주고받는 수수료로, 플러스면 상승 쪽에 돈이 몰렸다는 뜻",
    "미결제약정": "아직 청산되지 않은 선물 계약 규모",
    "숏 스퀴즈": "하락에 건 사람들이 손실을 막으려 급히 사들이며 가격이 더 뛰는 현상",
    "김프": "한국 거래소 가격이 해외보다 비싼 정도",
    "역프": "김프와 반대로, 한국 거래소 가격이 해외보다 싼 상태",
    "MDD": "고점 대비 최대 하락폭",
    "R²": "추세가 얼마나 일정한지, 1에 가까울수록 꾸준함",
    "순환매": "돈이 한 분야에서 다른 분야로 옮겨 다니며 차례로 오르는 현상",
    "정배열": "단기 평균선이 장기 평균선 위에 있는 상승 배열",
    "BTC 대비": "코인 가격을 비트코인 가격으로 나눠 본 상대 성적",
    "시가총액": "발행된 전체 주식·코인의 가치 합계로, 회사나 프로젝트의 덩치",
    "거래대금": "실제로 사고판 돈의 총액",
    "프리마켓": "미국 정규장이 열리기 전 거래 시간",
    "애프터마켓": "미국 정규장이 끝난 뒤 거래 시간",
    "상한가": "한국 주식이 하루에 오를 수 있는 최대치인 +30%",
    "언락": "묶여 있던 코인 물량이 시장에 풀리는 것",
    "바이백": "프로젝트나 회사가 자기 코인·주식을 되사들이는 것",
    "유상증자": "회사가 새 주식을 발행해 돈을 모으는 것으로, 기존 주식 가치가 묽어질 수 있음",
    "CB": "전환사채, 나중에 주식으로 바꿀 수 있는 채권",
    "대장주": "같은 테마 안에서 가장 먼저, 가장 크게 오르며 흐름을 이끄는 종목",
    "관찰 콜": "지금 사라는 신호가 아니라, 앞으로 움직임을 지켜볼 종목으로 표시해 두는 것",
    "시가": "그날 장이 열릴 때 처음 거래된 가격",
    "상관도": "두 종목이 같은 방향으로 움직인 정도로, 1에 가까울수록 함께 움직임",
    "%p": "퍼센트포인트, 두 등락률을 단순히 뺀 차이",
}

_ORDER = sorted(TERMS, key=len, reverse=True)
# 링크 / 인라인코드 / 굵게 를 한 덩어리로 떼어낸다 (나머지는 평문)
_SEG = re.compile(r"(<[^<>\n]+>|`[^`\n]+`|(?<![A-Za-z0-9*])\*[^*\n]+?\*(?![A-Za-z0-9*]))")


def _find(term: str, text: str) -> int:
    """용어 위치. 영숫자로 시작·끝나는 영문 약어는 앞뒤가 영숫자면 무시(ABCB 오탐 방지).
    더 긴 용어의 앞부분인 경우(예: '시가총액' 속 '시가')도 무시. 없으면 -1."""
    longer = [t for t in TERMS if t != term and t.startswith(term)]
    start = 0
    while True:
        i = text.find(term, start)
        if i < 0:
            return -1
        j = i + len(term)
        if term.isascii():
            if term[0].isalnum() and i > 0 and text[i - 1].isascii() and text[i - 1].isalnum():
                start = j
                continue
            if term[-1].isalnum() and j < len(text) and text[j].isascii() and text[j].isalnum():
                start = j
                continue
        if any(text.startswith(t, i) for t in longer):
            start = j
            continue
        return i


def _annotate_plain(text: str, used: set[str]) -> str:
    # 원문 기준으로 위치를 먼저 모두 찾고 뒤에서부터 끼워 넣는다
    # (풀이 문장 안의 다른 용어 — 예: '역프' 풀이 속 '김프' — 를 다시 풀지 않게)
    hits: list[tuple[int, int, str]] = []
    for term in _ORDER:
        if term in used:
            continue
        i = _find(term, text)
        if i < 0:
            continue
        j = i + len(term)
        if any(not (j <= a or i >= b) for a, b, _ in hits):
            continue  # 더 긴 용어와 겹침
        used.add(term)
        if text[j:j + 1] == "(":
            continue  # 이미 괄호 설명이 붙어 있음
        hits.append((i, j, term))
    for _i, j, term in sorted(hits, reverse=True):
        text = f"{text[:j]}({TERMS[term]}){text[j:]}"
    return text


def annotate_line(line: str, used: set[str]) -> str:
    out: list[str] = []
    for seg in _SEG.split(line):
        if not seg:
            continue
        if seg.startswith("<") or seg.startswith("`"):
            out.append(seg)
        elif seg.startswith("*") and seg.endswith("*") and len(seg) > 2:
            notes = []
            for term in _ORDER:
                if term not in used and _find(term, seg) >= 0:
                    notes.append(f"{term}: {TERMS[term]}")
                    used.add(term)
            out.append(seg + (f" ({'; '.join(notes)})" if notes else ""))
        else:
            out.append(_annotate_plain(seg, used))
    return "".join(out)


def annotate(lines: list[str], used: set[str] | None = None) -> list[str]:
    used = used if used is not None else set()
    return [annotate_line(ln, used) for ln in lines]
