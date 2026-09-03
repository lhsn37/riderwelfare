from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from .schedule_logic import KST, current_slot, schedule_label


APP_VERSION = "1.0.0"
CONTROL_TOKEN = os.getenv("CONTROL_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
STATE_PATH = Path(os.getenv("CONTROL_STATE_PATH", "/tmp/riderwelfare-control-state.json"))
STATE_LOCK = threading.Lock()
security = HTTPBasic()
app = FastAPI(title="Rider Welfare Auto Control", version=APP_VERSION)


DEFAULT_STATE = {
    "enabled": True,
    "manual_job_id": 0,
    "last_agent_id": None,
    "last_heartbeat_at": None,
    "last_status": "WAITING",
    "last_success_at": None,
    "last_error_at": None,
    "last_error": None,
    "last_slot": None,
    "last_message_preview": None,
}


def _load_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {**DEFAULT_STATE, **raw}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return dict(DEFAULT_STATE)


STATE = _load_state()


def _save_state() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def _require_agent(x_control_token: str = Header(default="")) -> None:
    if not CONTROL_TOKEN:
        raise HTTPException(503, "CONTROL_TOKEN이 설정되지 않았습니다.")
    if not secrets.compare_digest(x_control_token, CONTROL_TOKEN):
        raise HTTPException(401, "관제 실행기 인증에 실패했습니다.")


def _require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD가 설정되지 않았습니다.")
    user_ok = secrets.compare_digest(credentials.username.encode(), b"admin")
    password_ok = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="관리자 인증에 실패했습니다.",
            headers={"WWW-Authenticate": "Basic"},
        )


class PollInput(BaseModel):
    agent_id: str = Field(min_length=2, max_length=100)
    last_completed_slot: str | None = Field(default=None, max_length=80)
    last_manual_job_id: int = 0
    local_status: str = Field(default="READY", max_length=80)


class ReportInput(BaseModel):
    agent_id: str = Field(min_length=2, max_length=100)
    status: str = Field(max_length=80)
    slot: str | None = Field(default=None, max_length=80)
    message_preview: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, max_length=2000)


@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.post("/api/agent/poll", dependencies=[Depends(_require_agent)])
def agent_poll(body: PollInput):
    now = datetime.now(KST)
    with STATE_LOCK:
        STATE["last_agent_id"] = body.agent_id
        STATE["last_heartbeat_at"] = now.isoformat(timespec="seconds")
        STATE["last_status"] = body.local_status
        _save_state()

        if not STATE["enabled"]:
            return {"action": "WAIT", "reason": "PAUSED", "poll_after_seconds": 15}

        manual_job_id = int(STATE.get("manual_job_id") or 0)
        if manual_job_id > int(body.last_manual_job_id or 0):
            return {
                "action": "RUN",
                "kind": "MANUAL",
                "manual_job_id": manual_job_id,
                "slot": f"manual-{manual_job_id}",
            }

        slot = current_slot(now)
        if slot and slot != body.last_completed_slot:
            return {"action": "RUN", "kind": "SCHEDULED", "slot": slot}

        return {"action": "WAIT", "reason": "NOT_DUE", "poll_after_seconds": 15}


@app.post("/api/agent/report", dependencies=[Depends(_require_agent)])
def agent_report(body: ReportInput):
    now = datetime.now(KST).isoformat(timespec="seconds")
    with STATE_LOCK:
        STATE["last_agent_id"] = body.agent_id
        STATE["last_heartbeat_at"] = now
        STATE["last_status"] = body.status
        STATE["last_slot"] = body.slot
        if body.status == "SUCCESS":
            STATE["last_success_at"] = now
            STATE["last_error"] = None
            STATE["last_message_preview"] = body.message_preview
        elif body.status in {"ERROR", "LOGIN_REQUIRED", "KAKAO_REQUIRED"}:
            STATE["last_error_at"] = now
            STATE["last_error"] = body.error or body.status
        _save_state()
    return {"ok": True}


@app.post("/admin/toggle")
def toggle(_: None = Depends(_require_admin)):
    with STATE_LOCK:
        STATE["enabled"] = not bool(STATE["enabled"])
        _save_state()
    return RedirectResponse("/", status_code=303)


@app.post("/admin/send-now")
def send_now(_: None = Depends(_require_admin)):
    with STATE_LOCK:
        STATE["manual_job_id"] = int(time.time() * 1000)
        _save_state()
    return RedirectResponse("/", status_code=303)


def _safe(value) -> str:
    return html.escape(str(value or "-"))


@app.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(_require_admin)):
    with STATE_LOCK:
        state = dict(STATE)
    enabled = bool(state["enabled"])
    status_color = "#118a4e" if enabled else "#c0392b"
    status_text = "자동관제 실행중" if enabled else "자동관제 일시정지"
    return f"""
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="20"><title>라이더웰페어 자동관제</title>
<style>
body{{margin:0;background:#f4f6fb;color:#14213d;font-family:Arial,'Malgun Gothic',sans-serif}}main{{max-width:900px;margin:36px auto;padding:0 18px}}
h1{{margin:0 0 8px}}.sub{{color:#667085}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:22px 0}}
.card{{background:#fff;border:1px solid #e2e7f0;border-radius:16px;padding:20px;box-shadow:0 8px 24px rgba(20,33,61,.06)}}
.status{{font-size:20px;font-weight:900;color:{status_color}}}.label{{font-size:12px;color:#7b8494;margin-bottom:8px}}.value{{font-weight:800;word-break:break-all}}
.actions{{display:flex;gap:10px;flex-wrap:wrap}}button{{border:0;border-radius:11px;padding:13px 18px;font-weight:800;cursor:pointer;background:#2962ff;color:#fff}}
button.pause{{background:#111827}}pre{{white-space:pre-wrap;word-break:break-word;background:#0f172a;color:#e5e7eb;padding:16px;border-radius:12px}}
</style></head><body><main><h1>라이더웰페어 자동관제</h1><div class="sub">{schedule_label()} · 서버 v{APP_VERSION}</div>
<div class="grid"><section class="card"><div class="label">운영 상태</div><div class="status">{status_text}</div></section>
<section class="card"><div class="label">로컬 PC 마지막 연결</div><div class="value">{_safe(state['last_heartbeat_at'])}</div></section>
<section class="card"><div class="label">최근 전송 성공</div><div class="value">{_safe(state['last_success_at'])}</div></section>
<section class="card"><div class="label">로컬 상태</div><div class="value">{_safe(state['last_status'])}</div></section></div>
<section class="card"><div class="actions"><form method="post" action="/admin/send-now"><button>지금 즉시 전송</button></form>
<form method="post" action="/admin/toggle"><button class="pause">{'일시정지' if enabled else '자동관제 다시 시작'}</button></form></div>
<h3>최근 오류</h3><pre>{_safe(state['last_error'])}</pre><h3>최근 전송 메시지</h3><pre>{_safe(state['last_message_preview'])}</pre></section>
</main></body></html>"""
