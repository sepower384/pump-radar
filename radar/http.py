"""공용 HTTP 세션 — UA 위장, 재시도, 타임아웃, 병렬 헬퍼."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "ko,en;q=0.8"})


class FetchError(RuntimeError):
    pass


def get(url: str, *, params: dict | None = None, timeout: int = 15,
        retries: int = 2, headers: dict | None = None) -> requests.Response:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                last = FetchError(f"429 {url}")
                continue
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001 - 네트워크는 뭐든 올 수 있다
            last = e
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise FetchError(f"{url} :: {last}")


def get_json(url: str, **kw: Any) -> Any:
    return get(url, **kw).json()


def get_text(url: str, **kw: Any) -> str:
    r = get(url, **kw)
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def pmap(fn: Callable[[Any], Any], items: Sequence[Any], workers: int = 8) -> list:
    """실패한 항목은 None으로 채워 순서를 유지한다."""
    if not items:
        return []

    def safe(x: Any) -> Any:
        try:
            return fn(x)
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(safe, items))
