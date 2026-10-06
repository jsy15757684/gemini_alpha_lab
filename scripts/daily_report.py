#!/usr/bin/env python3
"""매일 아침 점검 보고 → 텔레그램. 서버의 systemd 타이머가 돌린다 (deploy/daily-report.sh).

맥 · Claude 앱 · 허가 창 없이 서버 혼자 돈다. 읽기만 한다 — 주문 · 봇 변경을 하지 않는다.

  해외 봇   봇 장부 수량 = 계좌 수량 인가 (라이브 전환 뒤 가장 중요한 확인)
            지난 24시간 체결
  서버      두 서비스 상태 · 지난 24시간 ERROR · 나무증권 토큰 남은 시간

데이터는 나무증권 워커(127.0.0.1:8889)에 내부 토큰으로 묻는다. 나무증권 API 를
직접 부르지 않는다 — 봇과 호출 한도를 나눠 쓰지 않고, 워커가 본 것을 그대로 본다.

텔레그램 설정은 .env 의 두 줄만 읽는다 (다른 키는 읽지 않는다):
  TELEGRAM_BOT_TOKEN=...   @BotFather 에서 만든 봇 토큰
  TELEGRAM_CHAT_ID=...     받을 대화 (--find-chat 으로 찾는다)

  python scripts/daily_report.py              보고서를 만들어 보낸다
  python scripts/daily_report.py --dry-run    보내지 않고 화면에만
  python scripts/daily_report.py --ping       연결 확인 메시지 한 줄
  python scripts/daily_report.py --find-chat  봇에게 말을 건 대화의 chat id 를 찾는다
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

ROOT = os.getenv("GAL_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

KST = ZoneInfo("Asia/Seoul")
ENV_FILE = os.path.join(ROOT, ".env")
TOKEN_FILE = os.path.join(ROOT, "data", "namuh_token.json")
SERVICES = ("bithumb-bot", "bithumb-namuh")
TG_LIMIT = 3900                      # 텔레그램 한 메시지 4,096자 — 여유를 둔다


# ── 설정 ──
def env_value(name: str) -> str:
    """환경변수 → 없으면 .env 에서 그 한 줄만."""
    v = (os.getenv(name) or "").strip()
    if v:
        return v
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                k, sep, val = line.strip().partition("=")
                if sep and k.strip() == name:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ── 가리기 — 오류 줄에 IP · 계좌번호 같은 긴 숫자가 섞여 나갈 수 있다 ──
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_LONG_NUM = re.compile(r"\b\d{9,}\b")
_SECRETISH = re.compile(r"(?i)(token|secret|key|authorization)[\"'=: ]+[A-Za-z0-9._\-]{12,}")


def mask(s: str) -> str:
    s = _IP.sub("<ip>", s)
    s = _LONG_NUM.sub("<숫자>", s)
    return _SECRETISH.sub(lambda m: m.group(1) + "=<숨김>", s)


# ── 데이터 모으기 (서버에서만) ──
def _worker(path: str) -> Any:
    from services import roles
    res = requests.get(roles.WORKER_URL + path, timeout=30,
                       headers={roles.INTERNAL_HEADER: roles.internal_token()})
    res.raise_for_status()
    return res.json()


def _service_states() -> Dict[str, str]:
    out = {}
    for s in SERVICES:
        try:
            out[s] = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True,
                                    timeout=10).stdout.strip() or "unknown"
        except Exception:
            out[s] = "unknown"
    return out


def _error_lines() -> Optional[List[str]]:
    """지난 24시간 ERROR · Traceback 줄. 로그를 못 읽으면 None."""
    cmd = ["journalctl", "--since", "24 hours ago", "--no-pager", "-o", "cat"]
    for s in SERVICES:
        cmd += ["-u", s]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0 or ("No journal files" in r.stderr) or ("not seeing messages" in r.stderr):
        return None
    return [ln for ln in r.stdout.splitlines() if "ERROR" in ln or "Traceback" in ln]


def _token_hours() -> Optional[float]:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return (float(json.load(f).get("expiresAt") or 0) - time.time()) / 3600
    except (OSError, ValueError):
        return None


def gather() -> Dict[str, Any]:
    data: Dict[str, Any] = {"now": datetime.now(KST).isoformat(), "services": _service_states(),
                            "errors": _error_lines(), "tokenHours": _token_hours(), "problems": []}
    for key, path in (("bots", "/api/bot/list"), ("trades", "/api/bot/trades"), ("account", "/api/namuh/account")):
        try:
            data[key] = _worker(path)
        except Exception as e:
            data[key] = None
            data["problems"].append(f"워커 {path} 조회 실패: {type(e).__name__}")
    return data


# ── 보고서 (순수 함수 — 시험은 여기만 본다) ──
def _recent(trades: List[Dict[str, Any]], now_local: datetime, hours: float = 24) -> List[Dict[str, Any]]:
    """체결 시각은 서버 시계(UTC) 기준 문자열이다."""
    out = []
    for t in trades:
        try:
            ts = datetime.strptime(str(t.get("time")), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if now_local - ts <= timedelta(hours=hours):
            out.append(t)
    return out


def build(data: Dict[str, Any], server_now: Optional[datetime] = None) -> str:
    now = datetime.fromisoformat(data["now"]).astimezone(KST)
    server_now = server_now or datetime.now()
    alerts: List[str] = list(data.get("problems") or [])
    lines: List[str] = []

    acc = data.get("account") or {}
    mode = "모의계좌" if acc.get("mock") else "⚠️ 실계좌"
    lines.append(f"📋 {now.strftime('%m/%d (%a) %H:%M')} 점검 · 나무증권 {mode}")

    # ── 해외 봇 ──
    bots = ((data.get("bots") or {}).get("bots")) or []
    trades = ((data.get("trades") or {}).get("trades")) or []
    held = {h.get("symbol"): float(h.get("quantity") or 0) for h in (acc.get("holdings") or [])}
    us = [b for b in bots if b.get("broker") == "namuh"]
    lines.append("")
    lines.append("🇺🇸 해외 봇")
    if not us:
        lines.append("  (봇 없음)")
    for b in us:
        led, real = float(b.get("units") or 0), held.get(b.get("coin"), 0.0)
        ok = abs(led - real) < 1e-6
        if not ok:
            alerts.append(f"{b['coin']} 장부 {led:g}주 ≠ 계좌 {real:g}주")
        run = "" if b.get("isRunning") else " · ⏸ 정지"
        if b.get("orderHold"):
            alerts.append(f"{b['coin']} 결과를 모르는 주문으로 멈춤")
            run += f" · 🛑 결과 모르는 주문으로 멈춤 ({(b['orderHold'] or {}).get('at', '')})"
        lines.append(f"  {'✅' if ok else '⚠️'} {b['coin']} 장부 {led:g} = 계좌 {real:g}주 · "
                     f"{b.get('turn')}/{(b.get('params') or {}).get('splitCount')}회차 · "
                     f"평가 {float(b.get('unrealizedPnlPct') or 0):+.2f}%{run}")
    us_ids = {b.get("botId") for b in us}
    us_recent = [t for t in _recent(trades, server_now) if t.get("botId") in us_ids]
    if us_recent:
        lines.append(f"  지난 24시간 체결 {len(us_recent)}건")
        for t in us_recent[:6]:
            lines.append(f"   · {t.get('coin')} {t.get('action')} {float(t.get('units') or 0):g}주 "
                         f"@ ${float(t.get('price') or 0):,.2f}")
    else:
        lines.append("  지난 24시간 체결 없음")
    if acc.get("buyingPower"):
        lines.append(f"  주문 가능 ${float(acc['buyingPower'].get('usd') or 0):,.0f}")

    # ── 서버 ──
    lines.append("")
    lines.append("🖥 서버")
    svc = data.get("services") or {}
    bad = [k for k, v in svc.items() if v != "active"]
    if bad:
        alerts.append("서비스 멈춤: " + ", ".join(f"{k}({svc[k]})" for k in bad))
    lines.append("  서비스 " + " · ".join(f"{k.replace('bithumb-', '')} {'✅' if v == 'active' else '❌ ' + v}"
                                          for k, v in svc.items()))
    errs = data.get("errors")
    if errs is None:
        lines.append("  로그를 읽지 못했습니다 (권한)")
    else:
        lines.append(f"  지난 24시간 ERROR {len(errs)}건")
        for ln in errs[-3:]:
            lines.append("   · " + mask(ln)[:160])
        if errs:
            alerts.append(f"ERROR {len(errs)}건")
    th = data.get("tokenHours")
    if th is not None:
        lines.append(f"  나무증권 토큰 {th:.1f}시간 남음")

    head = ("🚨 확인 필요: " + " · ".join(alerts)) if alerts else "✅ 이상 없음"
    return head + "\n" + "\n".join(lines)


# ── 텔레그램 ──
def _tg(method: str, token: str, **payload) -> Dict[str, Any]:
    """토큰이 든 주소가 예외 메시지로 로그에 남지 않게 감싼다."""
    try:
        res = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=payload, timeout=20)
        body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        raise RuntimeError(f"텔레그램 통신 실패 ({type(e).__name__})") from None
    if not body.get("ok"):
        raise RuntimeError(f"텔레그램 거부 {res.status_code}: {body.get('description', '')}")
    return body


def send(text: str) -> None:
    token, chat = env_value("TELEGRAM_BOT_TOKEN"), env_value("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError(".env 에 TELEGRAM_BOT_TOKEN · TELEGRAM_CHAT_ID 가 없습니다")
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > TG_LIMIT:
            parts.append(buf)
            buf = ""
        buf += line
    parts.append(buf)
    for p in parts:
        _tg("sendMessage", token, chat_id=chat, text=p, disable_web_page_preview="true")


def find_chat() -> None:
    token = env_value("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(".env 에 TELEGRAM_BOT_TOKEN 이 없습니다")
    ups = _tg("getUpdates", token).get("result") or []
    chats = {}
    for u in ups:
        c = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = c.get("title") or c.get("username") or c.get("first_name") or ""
    if not chats:
        print("받은 메시지가 없습니다. 텔레그램에서 봇에게 아무 말(예: /start)이나 보낸 뒤 다시 돌리세요.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


def main() -> int:
    args = set(sys.argv[1:])
    if "--find-chat" in args:
        find_chat()
        return 0
    if "--ping" in args:
        send("✅ gemini_alpha_lab 점검 알림이 연결됐습니다.")
        print("보냈습니다")
        return 0
    text = build(gather())
    if "--dry-run" in args:
        print(text)
        return 0
    send(text)
    print(text.splitlines()[0])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
