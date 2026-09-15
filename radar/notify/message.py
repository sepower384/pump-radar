"""슬랙·텔레그램 공용 메시지 모델.

본문은 슬랙 mrkdwn 줄로 한 번만 만들고(단일 원본),
  - 슬랙: 기존처럼 text + blocks
  - 텔레그램: 줄마다 HTML 로 변환 후 '단위(코인/종목 하나)' 경계로 4096자 분할
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import glossary
from . import telegram

# 텔레그램 토픽 이름 (헤더에 그대로 쓴다)
TOPIC_TITLE = {
    "pump": "🚀 세력의 급등 알림",
    "trend": "📈 세력의 BTC차트 감별",
    "stock": "💹 세력의 주식 급등판",
}
BOT_NAME = "세력의 비서실장"


@dataclass
class Msg:
    kind: str
    subtitle: str                                     # 예: "지금 급하게 움직이는 코인 3개"
    units: list[list[str]] = field(default_factory=list)   # [0]=도입부, 이후 코인/종목 하나씩
    footer: list[str] = field(default_factory=list)
    photo: str = ""                                   # quickchart URL (없으면 사진 생략)
    caption: str = ""                                 # 사진 캡션 (텔레그램 HTML, 1~2줄)
    summary: str = ""                                 # 슬랙 알림 미리보기(푸시) 한 줄
    annotated: bool = False

    @property
    def title(self) -> str:
        return TOPIC_TITLE.get(self.kind, self.kind)

    def annotate(self) -> "Msg":
        """용어 풀이를 메시지 전체에서 처음 한 번만 붙인다(도입부 → 본문 → 꼬리말 순)."""
        if not self.annotated:
            used: set[str] = set()
            self.units = [glossary.annotate(u, used) for u in self.units]
            self.footer = glossary.annotate(self.footer, used)
            self.annotated = True
        return self

    # ── 슬랙 ──
    def slack_lines(self) -> list[str]:
        lines: list[str] = []
        for u in self.units:
            lines += u + [""]
        return lines

    def slack_header(self) -> str:
        return f"{self.title} · {self.subtitle}" if self.subtitle else self.title

    def slack_text(self) -> str:
        self.annotate()
        head = self.summary or self.slack_header()
        return head + "\n" + "\n".join(self.slack_lines() + self.footer)

    def slack_blocks(self) -> list:
        self.annotate()
        return slack_blocks(self.slack_header(), self.slack_lines(), "\n".join(self.footer))

    # ── 텔레그램 ──
    def telegram_units(self) -> list[str]:
        self.annotate()
        head = f"<b>{telegram.to_html(self.title)}</b>"
        if self.subtitle:
            head += f"\n{telegram.to_html(self.subtitle)}"
        units = [head] + [telegram.to_html("\n".join(u)) for u in self.units if u]
        if self.footer:
            units.append(telegram.to_html("\n".join(self.footer)))
        return units

    def telegram_chunks(self) -> list[str]:
        return telegram.split_messages(self.telegram_units())


def slack_blocks(header: str, body_lines: list[str], footer: str = "") -> list:
    blocks: list = [{"type": "header", "text": {"type": "plain_text", "text": header[:150],
                                                "emoji": True}}]
    chunk: list[str] = []
    size = 0
    for line in body_lines:
        if size + len(line) > 2700 and chunk:
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks
