# -*- coding: utf-8 -*-
r"""아침 보고 정시 요청기 — 일찍 깨어나 정시까지 기다렸다가 kwon-gowidnexus/claude 의 아침 보고 워크플로를 workflow_dispatch 로
요청한다 (2026-10-28, KYI "첫째 안 진행해").

배경: 아침 보고 세 개의 정시 요청은 개인 PC 작업 스케줄러가 보내는데 PC가 꺼진 날은 0건이다. GitHub 정기 실행은 79~289분 늦게
뜨고(2026-10-27 실측 30회), 자주 뜨는 예약은 칸이 버려져 간격과 무관하게 하루 2~3회만 뜬다(5분·15분·20분 간격 모두,
2026-10-26~28 실측). 그래서 '늦게 떠도 상관없게' — 새벽에 뜨는 하루 한 번의 작업이 실제 시각을 보고 정시까지 잠든 뒤 요청한다.
요청(workflow_dispatch)은 예약과 달리 몇 초 안에 시작한다. 이 저장소는 공개라 대기 시간이 무료다.

판단(대상마다, 정시 순):
  ① 오늘(KST 0시 이후) 진행 중이거나 성공으로 끝난 실행이 있으면 건너뛴다 — PC 정시 요청·예비 cron·수동 실행·감시 작업 모두 센다
  ② 정시 전이면 정시까지 잠든다. 깬 뒤 ①을 다시 본다(잠든 사이 PC가 요청했을 수 있다)
  ③ 요청하고(ref=main) 60초 안에 새 실행이 생긴 것을 확인한다
실행 목록을 못 읽으면 '오늘 실행 없음'으로 보고 요청한다 — 중복은 대상 워크플로의 같은 날 가드가 막고, 요청 한 번의 비용은
비공개 저장소 분량 몇 분이다. 요청이 거절되거나 실행이 안 생기면 그 대상만 실패로 적고 다음 대상으로 간다.

용법:
  python3 trigger.py --repo kwon-gowidnexus/claude            # 판단 + 대기 + 요청
  python3 trigger.py --now                                     # 기다리지 않고 바로 판단·요청(검증용)
  python3 trigger.py --now --force newsletter-digest.yml       # ①과 무관하게 요청(실가동 검증용 — 같은 날 가드가 발송을 막는다)
  python3 trigger.py --targets daily-fortune.yml               # 일부 대상만
환경변수 MORNING_TRIGGER_TOKEN: claude 저장소 한정 · Actions 읽기/쓰기 권한의 세분화 토큰.
종료 코드: 0 = 정상(건너뜀 포함) · 1 = 요청 실패 있음 · 2 = 토큰 없음.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone

TARGETS = {"daily-fortune.yml": "06:30", "newsletter-digest.yml": "07:00", "checkpoint-reporter.yml": "08:00"}  # 정시(KST)
KST = timezone(timedelta(hours=9))
API = "https://api.github.com"
DEFAULT_REPO = "kwon-gowidnexus/claude"
DEFAULT_REF = "main"
DONE = {"success", "neutral", "skipped"}   # 끝났고 '보낸 것'으로 치는 결론
CONFIRM_WINDOW_S = 60                      # 요청 뒤 실행 생성 확인 창
CONFIRM_POLL_S = 5
MAX_WAIT_S = 6 * 3600                      # 작업당 상한(6시간) 안에서만 잠든다
LIST_SINCE_SLACK_S = 30                    # 요청 직후 생성 확인 때 시계 오차 여유


class ListError(Exception):
    """실행 목록을 못 읽었다."""


class DispatchError(Exception):
    """요청이 거절됐거나 실행이 생기지 않았다."""


def log(msg: str) -> None:
    print(f"[trigger] {msg}", flush=True)


# ── 시각 ────────────────────────────────────────────────────────────────────

def due_at(now_kst: datetime, hhmm: str) -> datetime:
    """오늘(KST) 정시."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(now_kst.astimezone(KST).date(), dtime(h, m), KST)


def day_start_utc(now_kst: datetime) -> datetime:
    """오늘(KST) 0시를 UTC로."""
    return datetime.combine(now_kst.astimezone(KST).date(), dtime(0, 0), KST).astimezone(timezone.utc)


def _created(run: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return None


def _hhmm(dt: datetime | None) -> str:
    return dt.astimezone(KST).strftime("%H:%M") if dt else "?"


# ── 판단 ────────────────────────────────────────────────────────────────────

def has_live_run_today(runs: list, now_kst: datetime) -> bool:
    """오늘(KST 0시 이후) 만들어진 실행 중 진행 중이거나 성공으로 끝난 것이 있는가."""
    start = day_start_utc(now_kst)
    for r in runs:
        c = _created(r)
        if c is None or c < start:
            continue
        if r.get("status") != "completed" or r.get("conclusion") in DONE:
            return True
    return False


def _live_desc(runs: list, now_kst: datetime) -> str:
    start = day_start_utc(now_kst)
    for r in runs:
        c = _created(r)
        if c is not None and c >= start and (r.get("status") != "completed" or r.get("conclusion") in DONE):
            return f"{r.get('event')} {_hhmm(c)} {r.get('status')}/{r.get('conclusion')}"
    return ""


def decide(wf: str, now_kst: datetime, runs: list, force: bool = False, immediate: bool = False) -> tuple[str, int, str]:
    """('skip'|'dispatch', 대기 초, 사유)."""
    now_kst = now_kst.astimezone(KST)
    if not force and has_live_run_today(runs, now_kst):
        return "skip", 0, f"오늘 실행 있음 — {_live_desc(runs, now_kst)}"
    due = due_at(now_kst, TARGETS[wf])
    wait = 0 if immediate else max(0, int((due - now_kst).total_seconds()))
    if immediate:
        reason = f"즉시 요청(--now) · 정시 {TARGETS[wf]}"
    elif wait > 0:
        reason = f"정시 {TARGETS[wf]}까지 {wait // 60}분 대기 뒤 요청"
    else:
        reason = f"정시 지남({TARGETS[wf]}) · 오늘 실행 없음 → 즉시 요청"
    if force:
        reason = "강제 요청(--force) · " + reason
    return "dispatch", wait, reason


def run_target(wf: str, *, now_fn, sleep_fn, list_runs_fn, dispatch_fn, force: bool = False, immediate: bool = False) -> str:
    """대상 하나: 판단 → (대기 → 재확인) → 요청. 'dispatched' | 'skipped' | 'failed'."""

    def runs_now() -> list:
        try:
            return list_runs_fn(wf, now_fn())
        except ListError as e:
            log(f"{wf} 실행 목록 조회 실패({e}) — 오늘 실행 없음으로 보고 진행(중복은 대상의 같은 날 가드가 막는다)")
            return []

    action, wait, reason = decide(wf, now_fn(), runs_now(), force=force, immediate=immediate)
    log(f"{wf} 정시 {TARGETS[wf]} — {'건너뜀' if action == 'skip' else '요청'}: {reason} (지금 {_hhmm(now_fn())} KST)")
    if action == "skip":
        return "skipped"
    if wait > 0:
        if wait > MAX_WAIT_S:
            log(f"{wf} 대기 {wait // 60}분이 상한 {MAX_WAIT_S // 60}분을 넘어 상한까지만 기다린다")
            wait = MAX_WAIT_S
        sleep_fn(wait)
        action2, _, reason2 = decide(wf, now_fn(), runs_now(), force=force, immediate=True)
        if action2 == "skip":
            log(f"{wf} 깬 뒤 재확인 — 건너뜀: {reason2} (지금 {_hhmm(now_fn())} KST)")
            return "skipped"
        log(f"{wf} 깬 뒤 재확인 — 오늘 실행 없음, 요청한다 (지금 {_hhmm(now_fn())} KST)")
    try:
        run_id = dispatch_fn(wf)
    except DispatchError as e:
        log(f"{wf} 요청 실패: {e}")
        return "failed"
    log(f"{wf} 요청 완료 — 실행 {run_id} 생성 확인 (지금 {_hhmm(now_fn())} KST)")
    return "dispatched"


# ── GitHub API ──────────────────────────────────────────────────────────────

def make_http(token: str):
    """http(method, url, body) -> (status, json). 네트워크 오류는 status 0."""

    def http(method: str, url: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "morning-trigger",
        })
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, {"message": str(e)}
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = {"raw": raw[:200].decode("utf-8", errors="replace")}
        return status, payload

    return http


def list_runs_today(wf: str, repo: str, now_kst: datetime, *, http) -> list:
    """오늘(KST 0시 이후) 만들어진 이 워크플로의 실행 목록. 못 읽으면 ListError."""
    since = day_start_utc(now_kst).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = urllib.parse.urlencode({"created": f">={since}", "per_page": 50})
    status, payload = http("GET", f"{API}/repos/{repo}/actions/workflows/{wf}/runs?{q}")
    if status != 200 or not isinstance(payload, dict):
        raise ListError(f"{status} {str(payload)[:200]}")
    return list(payload.get("workflow_runs") or [])


def dispatch(wf: str, repo: str, *, http, now_fn, sleep_fn, ref: str = DEFAULT_REF) -> int:
    """workflow_dispatch 요청 → CONFIRM_WINDOW_S 안에 새 실행이 생겼는지 확인 → 실행 id. 실패하면 DispatchError."""
    t0 = now_fn().astimezone(timezone.utc)
    status, payload = http("POST", f"{API}/repos/{repo}/actions/workflows/{wf}/dispatches", {"ref": ref})
    if status != 204:
        raise DispatchError(f"요청 거절 {status} {str(payload)[:200]}")
    floor = t0 - timedelta(seconds=LIST_SINCE_SLACK_S)
    since = floor.strftime("%Y-%m-%dT%H:%M:%SZ")
    waited = 0
    while True:
        q = urllib.parse.urlencode({"event": "workflow_dispatch", "created": f">={since}", "per_page": 10})
        status, payload = http("GET", f"{API}/repos/{repo}/actions/workflows/{wf}/runs?{q}")
        if status == 200 and isinstance(payload, dict):
            for r in payload.get("workflow_runs") or []:
                c = _created(r)
                if c is not None and c >= floor:
                    return int(r.get("id", 0))
        if waited >= CONFIRM_WINDOW_S:
            raise DispatchError(f"요청은 받았으나(204) {CONFIRM_WINDOW_S}초 안에 실행이 생기지 않음(마지막 조회 {status})")
        sleep_fn(CONFIRM_POLL_S)
        waited += CONFIRM_POLL_S


# ── 진입 ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="아침 보고 정시 요청기 — 정시까지 기다렸다가 workflow_dispatch 로 요청")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--now", action="store_true", help="정시까지 기다리지 않고 바로 판단·요청(검증용)")
    ap.add_argument("--force", default="", help="쉼표로 적은 워크플로를 오늘 실행 여부와 무관하게 요청(검증용)")
    ap.add_argument("--targets", default="", help="쉼표로 적은 대상만 처리(비우면 전부)")
    a = ap.parse_args(argv)
    force = {w.strip() for w in a.force.split(",") if w.strip()}
    targets = [w.strip() for w in a.targets.split(",") if w.strip()] or list(TARGETS)
    unknown = (set(targets) | force) - set(TARGETS)
    if unknown:
        ap.error(f"목록 밖 워크플로: {', '.join(sorted(unknown))} (허용 {', '.join(TARGETS)})")
    token = os.environ.get("MORNING_TRIGGER_TOKEN", "").strip()   # 비밀값을 stdin으로 넣으면 끝에 줄바꿈이 붙을 수 있다
    if not token:
        log("MORNING_TRIGGER_TOKEN 이 없다 — 요청할 수 없어 종료(2)")
        return 2
    http = make_http(token)

    def now_fn() -> datetime:
        return datetime.now(timezone.utc).astimezone(KST)

    ordered = sorted(targets, key=lambda w: TARGETS[w])
    log(f"지금 {now_fn():%Y-%m-%d %H:%M} KST · 대상 {', '.join(ordered)}"
        f"{' · 즉시(--now)' if a.now else ''}{' · 강제 ' + ','.join(sorted(force)) if force else ''}")
    results: dict[str, str] = {}
    for wf in ordered:
        results[wf] = run_target(
            wf,
            now_fn=now_fn,
            sleep_fn=time.sleep,
            list_runs_fn=lambda w, now: list_runs_today(w, a.repo, now, http=http),
            dispatch_fn=lambda w: dispatch(w, a.repo, http=http, now_fn=now_fn, sleep_fn=time.sleep, ref=a.ref),
            force=wf in force,
            immediate=a.now,
        )
    log("결과: " + ", ".join(f"{w}={r}" for w, r in results.items()))
    return 1 if any(r == "failed" for r in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
