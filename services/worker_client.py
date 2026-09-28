"""crypto 프로세스가 나무증권 워커를 부르는 통로.

워커는 127.0.0.1 에서만 받고, 내부 토큰이 붙은 요청만 처리한다. 화면의
로그인 확인은 crypto 쪽이 이미 했으므로 워커는 토큰만 본다.
"""

from typing import Any, Dict, Optional, Tuple

import requests

from services import roles


class WorkerDown(RuntimeError):
    """워커에 닿지 못했다 (꺼졌거나 재시작 중)."""


def call(method: str, path: str, *, json: Any = None, params: Optional[Dict[str, Any]] = None,
         body: Optional[bytes] = None, content_type: Optional[str] = None,
         timeout: float = 20.0) -> Tuple[int, Any, bytes]:
    """워커에 요청을 보내 (상태코드, JSON 또는 None, 원문) 을 돌려준다."""
    headers = {roles.INTERNAL_HEADER: roles.internal_token()}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        res = requests.request(method, f"{roles.WORKER_URL}{path}", headers=headers,
                               json=json, params=params, data=body, timeout=timeout)
    except requests.RequestException as e:
        raise WorkerDown(f"나무증권 프로세스에 연결하지 못했습니다 ({roles.WORKER_URL}): "
                         f"{type(e).__name__}") from e
    try:
        parsed = res.json()
    except ValueError:
        parsed = None
    return res.status_code, parsed, res.content


def get_json(path: str, timeout: float = 20.0, **params) -> Dict[str, Any]:
    """성공(2xx)한 JSON 만 돌려준다. 실패는 WorkerDown 으로 올린다."""
    status, parsed, raw = call("GET", path, params=params or None, timeout=timeout)
    if status >= 300 or not isinstance(parsed, dict):
        detail = (parsed or {}).get("detail") if isinstance(parsed, dict) else raw[:120]
        raise WorkerDown(f"나무증권 프로세스가 {path} 에 HTTP {status} 로 답했습니다: {detail}")
    return parsed
