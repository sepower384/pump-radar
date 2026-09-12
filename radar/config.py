"""설정/환경변수 로딩. 외부 의존성 없음(.env 직접 파싱)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_env() -> None:
    """.env 파일을 os.environ에 반영 (이미 있는 값은 덮어쓰지 않음)."""
    for name in (".env", ".env.local"):
        p = ROOT / name
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


_load_env()


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (ROOT / "config.json")
        self._d: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8-sig"))

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._d
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> dict:
        v = self._d.get(name)
        return v if isinstance(v, dict) else {}


def env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


CFG = Config()
