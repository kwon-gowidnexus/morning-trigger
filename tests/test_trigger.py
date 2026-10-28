# -*- coding: utf-8 -*-
"""trigger.py 판단·대기·요청 로직 시험 (가짜 시계·가짜 HTTP, 실제 네트워크 없음)."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import trigger as T  # noqa: E402

KST = timezone(timedelta(hours=9))
REPO = "kwon-gowidnexus/claude"


def kst(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=KST)


def run(created_kst: datetime, status="completed", conclusion="success", event="schedule", run_id=1):
    return {"id": run_id, "created_at": created_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status, "conclusion": conclusion, "event": event}


# ── 시각 계산 ──────────────────────────────────────────────────────────────

def test_due_at_builds_today_kst_time():
    now = kst(2026, 10, 29, 4, 10)
    due = T.due_at(now, "06:30")
    assert due == kst(2026, 10, 29, 6, 30)
    assert due.tzinfo is not None


def test_day_start_utc_is_kst_midnight():
    now = kst(2026, 10, 29, 4, 10)
    assert T.day_start_utc(now) == datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


# ── 오늘 실행 판정 ───────────────────────────────────────────────────────────

def test_has_live_run_today_true_for_in_progress():
    now = kst(2026, 10, 29, 6, 31)
    runs = [run(kst(2026, 10, 29, 6, 30), status="in_progress", conclusion=None)]
    assert T.has_live_run_today(runs, now) is True


def test_has_live_run_today_true_for_success():
    now = kst(2026, 10, 29, 9, 0)
    runs = [run(kst(2026, 10, 29, 6, 31), status="completed", conclusion="success")]
    assert T.has_live_run_today(runs, now) is True


def test_has_live_run_today_false_for_failed_only():
    now = kst(2026, 10, 29, 9, 0)
    runs = [run(kst(2026, 10, 29, 6, 31), status="completed", conclusion="failure")]
    assert T.has_live_run_today(runs, now) is False


def test_has_live_run_today_ignores_yesterday_runs():
    now = kst(2026, 10, 29, 6, 0)
    runs = [run(kst(2026, 10, 28, 9, 41), status="completed", conclusion="success")]
    assert T.has_live_run_today(runs, now) is False


# ── 판단 ───────────────────────────────────────────────────────────────────

def test_decide_skips_when_live_run_today():
    now = kst(2026, 10, 29, 6, 0)
    runs = [run(kst(2026, 10, 29, 5, 50), status="in_progress", conclusion=None)]
    action, wait_s, reason = T.decide("daily-fortune.yml", now, runs)
    assert action == "skip"
    assert wait_s == 0
    assert "오늘 실행 있음" in reason


def test_decide_waits_until_due_when_before_due():
    now = kst(2026, 10, 29, 4, 10)
    action, wait_s, reason = T.decide("daily-fortune.yml", now, [])
    assert action == "dispatch"
    assert wait_s == 140 * 60
    assert "06:30" in reason


def test_decide_dispatches_immediately_when_past_due():
    now = kst(2026, 10, 29, 7, 5)
    action, wait_s, reason = T.decide("daily-fortune.yml", now, [])
    assert action == "dispatch"
    assert wait_s == 0
    assert "정시 지남" in reason


def test_decide_force_ignores_existing_runs():
    now = kst(2026, 10, 29, 9, 0)
    runs = [run(kst(2026, 10, 29, 6, 31))]
    action, wait_s, _ = T.decide("daily-fortune.yml", now, runs, force=True)
    assert action == "dispatch"
    assert wait_s == 0


def test_decide_immediate_sets_zero_wait_even_before_due():
    now = kst(2026, 10, 29, 4, 10)
    action, wait_s, _ = T.decide("daily-fortune.yml", now, [], immediate=True)
    assert action == "dispatch"
    assert wait_s == 0


# ── 대상 하나 처리(대기 → 재확인 → 요청) ───────────────────────────────────────

class FakeClock:
    def __init__(self, start: datetime):
        self.now = start
        self.slept = []

    def now_fn(self):
        return self.now

    def sleep_fn(self, seconds):
        self.slept.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)


def test_run_target_sleeps_until_due_then_dispatches():
    clock = FakeClock(kst(2026, 10, 29, 4, 10))
    dispatched = []
    result = T.run_target("daily-fortune.yml", now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
                          list_runs_fn=lambda wf, now: [], dispatch_fn=lambda wf: dispatched.append(wf) or 123)
    assert clock.slept == [140 * 60]
    assert dispatched == ["daily-fortune.yml"]
    assert result == "dispatched"


def test_run_target_rechecks_after_waking_and_skips_if_run_appeared():
    clock = FakeClock(kst(2026, 10, 29, 4, 10))
    calls = {"n": 0}

    def list_runs(wf, now):
        calls["n"] += 1
        # 첫 조회(잠들기 전)는 없음, 둘째 조회(깬 뒤)는 PC가 06:30에 요청한 실행이 있음
        if calls["n"] == 1:
            return []
        return [run(kst(2026, 10, 29, 6, 30), status="in_progress", conclusion=None, event="workflow_dispatch")]

    dispatched = []
    result = T.run_target("daily-fortune.yml", now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
                          list_runs_fn=list_runs, dispatch_fn=lambda wf: dispatched.append(wf) or 1)
    assert clock.slept == [140 * 60]
    assert dispatched == []
    assert result == "skipped"
    assert calls["n"] == 2


def test_run_target_skips_without_sleeping_when_run_exists():
    clock = FakeClock(kst(2026, 10, 29, 4, 10))
    runs = [run(kst(2026, 10, 29, 2, 0), status="completed", conclusion="success")]
    result = T.run_target("daily-fortune.yml", now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
                          list_runs_fn=lambda wf, now: runs, dispatch_fn=lambda wf: 1)
    assert clock.slept == []
    assert result == "skipped"


def test_run_target_treats_list_failure_as_no_run_and_dispatches():
    clock = FakeClock(kst(2026, 10, 29, 7, 0))

    def list_runs(wf, now):
        raise T.ListError("403")

    dispatched = []
    result = T.run_target("daily-fortune.yml", now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
                          list_runs_fn=list_runs, dispatch_fn=lambda wf: dispatched.append(wf) or 1)
    assert dispatched == ["daily-fortune.yml"]
    assert result == "dispatched"


def test_run_target_returns_failed_when_dispatch_raises():
    clock = FakeClock(kst(2026, 10, 29, 7, 0))

    def dispatch(wf):
        raise T.DispatchError("422 no workflow_dispatch")

    result = T.run_target("daily-fortune.yml", now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
                          list_runs_fn=lambda wf, now: [], dispatch_fn=dispatch)
    assert result == "failed"


# ── GitHub 요청(가짜 HTTP) ───────────────────────────────────────────────────

class FakeHttp:
    """http(method, url, body) -> (status, json). 호출 기록을 남긴다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        status, payload = self.responses.pop(0)
        return status, payload


def test_dispatch_posts_ref_main_and_confirms_new_run():
    clock = FakeClock(kst(2026, 10, 29, 6, 30))
    new_run = run(kst(2026, 10, 29, 6, 30, ), status="queued", conclusion=None, event="workflow_dispatch", run_id=777)
    http = FakeHttp([(204, None), (200, {"workflow_runs": []}), (200, {"workflow_runs": [new_run]})])
    run_id = T.dispatch("daily-fortune.yml", REPO, http=http, now_fn=clock.now_fn, sleep_fn=clock.sleep_fn)
    assert run_id == 777
    method, url, body = http.calls[0]
    assert method == "POST"
    assert url.endswith("/repos/kwon-gowidnexus/claude/actions/workflows/daily-fortune.yml/dispatches")
    assert body == {"ref": "main"}
    assert all(c[0] == "GET" for c in http.calls[1:])


def test_dispatch_raises_when_post_rejected():
    clock = FakeClock(kst(2026, 10, 29, 6, 30))
    http = FakeHttp([(404, {"message": "Not Found"})])
    with pytest.raises(T.DispatchError):
        T.dispatch("daily-fortune.yml", REPO, http=http, now_fn=clock.now_fn, sleep_fn=clock.sleep_fn)


def test_dispatch_raises_when_no_run_appears_within_confirm_window():
    clock = FakeClock(kst(2026, 10, 29, 6, 30))
    http = FakeHttp([(204, None)] + [(200, {"workflow_runs": []})] * 50)
    with pytest.raises(T.DispatchError):
        T.dispatch("daily-fortune.yml", REPO, http=http, now_fn=clock.now_fn, sleep_fn=clock.sleep_fn)
    assert sum(clock.slept) >= T.CONFIRM_WINDOW_S


def test_dispatch_ignores_runs_created_before_the_request():
    clock = FakeClock(kst(2026, 10, 29, 6, 30))
    old = run(kst(2026, 10, 29, 6, 0), status="completed", conclusion="success", event="workflow_dispatch", run_id=5)
    http = FakeHttp([(204, None)] + [(200, {"workflow_runs": [old]})] * 50)
    with pytest.raises(T.DispatchError):
        T.dispatch("daily-fortune.yml", REPO, http=http, now_fn=clock.now_fn, sleep_fn=clock.sleep_fn)


def test_list_runs_today_queries_created_since_kst_midnight():
    now = kst(2026, 10, 29, 6, 30)
    http = FakeHttp([(200, {"workflow_runs": [{"id": 1}]})])
    runs = T.list_runs_today("daily-fortune.yml", REPO, now, http=http)
    assert runs == [{"id": 1}]
    method, url, _ = http.calls[0]
    assert method == "GET"
    assert "created=%3E%3D2026-10-28T15%3A00%3A00Z" in url or "created=>=2026-10-28T15:00:00Z" in url


def test_list_runs_today_raises_list_error_on_http_failure():
    now = kst(2026, 10, 29, 6, 30)
    http = FakeHttp([(403, {"message": "forbidden"})])
    with pytest.raises(T.ListError):
        T.list_runs_today("daily-fortune.yml", REPO, now, http=http)


# ── main: 순서·필터·종료 코드 ─────────────────────────────────────────────────

def test_main_processes_targets_in_due_order_and_returns_zero(monkeypatch):
    order = []

    def fake_run_target(wf, **kw):
        order.append(wf)
        return "dispatched"

    monkeypatch.setattr(T, "run_target", fake_run_target)
    monkeypatch.setenv("MORNING_TRIGGER_TOKEN", "x")
    rc = T.main(["--repo", REPO, "--now"])
    assert rc == 0
    assert order == ["daily-fortune.yml", "newsletter-digest.yml", "checkpoint-reporter.yml"]


def test_main_targets_filter_limits_to_named_workflows(monkeypatch):
    order = []
    monkeypatch.setattr(T, "run_target", lambda wf, **kw: order.append(wf) or "skipped")
    monkeypatch.setenv("MORNING_TRIGGER_TOKEN", "x")
    rc = T.main(["--repo", REPO, "--now", "--targets", "newsletter-digest.yml"])
    assert rc == 0
    assert order == ["newsletter-digest.yml"]


def test_main_rejects_unknown_target(monkeypatch):
    monkeypatch.setenv("MORNING_TRIGGER_TOKEN", "x")
    with pytest.raises(SystemExit):
        T.main(["--repo", REPO, "--targets", "nope.yml"])


def test_main_returns_one_when_any_target_failed(monkeypatch):
    results = iter(["dispatched", "failed", "skipped"])
    monkeypatch.setattr(T, "run_target", lambda wf, **kw: next(results))
    monkeypatch.setenv("MORNING_TRIGGER_TOKEN", "x")
    assert T.main(["--repo", REPO, "--now"]) == 1


def test_main_requires_token(monkeypatch):
    monkeypatch.delenv("MORNING_TRIGGER_TOKEN", raising=False)
    assert T.main(["--repo", REPO, "--now"]) == 2


def test_main_force_passes_force_only_to_named_targets(monkeypatch):
    seen = {}
    monkeypatch.setattr(T, "run_target", lambda wf, **kw: seen.__setitem__(wf, kw.get("force")) or "dispatched")
    monkeypatch.setenv("MORNING_TRIGGER_TOKEN", "x")
    T.main(["--repo", REPO, "--now", "--force", "newsletter-digest.yml"])
    assert seen == {"daily-fortune.yml": False, "newsletter-digest.yml": True, "checkpoint-reporter.yml": False}


# ── 워크플로 파일 덮개 ───────────────────────────────────────────────────────

def test_workflow_cron_wakes_at_0230_kst_and_job_ends_inside_six_hours():
    path = os.path.join(ROOT, ".github", "workflows", "morning-trigger.yml")
    with open(path, encoding="utf-8") as f:
        yml = f.read()
    assert re.search(r"cron:\s*'30 17 \* \* \*'", yml), "cron은 UTC 17:30 = KST 02:30 한 줄이어야 한다"
    m = re.search(r"timeout-minutes:\s*(\d+)", yml)
    assert m and int(m.group(1)) <= 350
    assert "workflow_dispatch" in yml
    assert "MORNING_TRIGGER_TOKEN" in yml
    assert "contents: write" in yml  # 활동 커밋(60일 무활동 자동 중지 방지)


def test_targets_match_private_repo_due_times():
    assert T.TARGETS == {"daily-fortune.yml": "06:30", "newsletter-digest.yml": "07:00", "checkpoint-reporter.yml": "08:00"}
