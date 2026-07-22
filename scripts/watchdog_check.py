
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_watchdog import RESTART_EXIT_CODE, GatewayWatchdog  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(tmp_dir: str):
    clock = FakeClock()
    alerts: list[str] = []
    exits: list[int] = []
    watchdog = GatewayWatchdog(
        dump_dir=Path(tmp_dir) / "watchdog",
        clock=clock,
        alert_fn=lambda text: alerts.append(text) or True,
        exit_fn=lambda code: exits.append(code),
        poll_stall_seconds=150.0,
        turn_warn_seconds=180.0,
        turn_exit_seconds=600.0,
    )
    return watchdog, clock, alerts, exits


def check_healthy_loop_never_fires(tmp_dir: str) -> None:
    watchdog, clock, alerts, exits = build(tmp_dir)
    watchdog.beat("tg_get_updates")
    for _ in range(20):
        clock.advance(25.0)
        watchdog.beat("tg_get_updates")
        assert watchdog.check_once() == [], "healthy heartbeat must not trigger incidents"
    assert not alerts and not exits


def check_poll_stall_restarts(tmp_dir: str) -> None:
    watchdog, clock, alerts, exits = build(tmp_dir)
    watchdog.beat("tg_get_updates")
    clock.advance(151.0)
    incidents = watchdog.check_once()
    assert incidents == ["poll_stalled"], incidents
    assert exits == [RESTART_EXIT_CODE]
    assert len(alerts) == 1 and "重啟" in alerts[0]
    dumps = list((Path(tmp_dir) / "watchdog").glob("stall_*.log"))
    assert dumps, "poll stall must write a stack dump"
    content = dumps[0].read_text(encoding="utf-8")
    assert "poll_stalled" in content and "all thread stacks" in content


def check_no_poll_beat_means_no_restart(tmp_dir: str) -> None:
    watchdog, clock, alerts, exits = build(tmp_dir)
    clock.advance(9999.0)
    assert watchdog.check_once() == [], "polling never started; watchdog must stay quiet"
    assert not exits


def check_turn_warn_fires_once(tmp_dir: str) -> None:
    watchdog, clock, alerts, exits = build(tmp_dir)
    watchdog.beat("tg_get_updates")
    token = watchdog.begin_turn(123, 456)
    clock.advance(181.0)
    watchdog.beat("tg_get_updates")
    incidents = watchdog.check_once()
    assert incidents == ["turn_stuck_warn"], incidents
    # Owner 2026-07-22: the stall alert must be IN VOICE, not a crash log. Forensics still go to
    # the trace/dump; the owner only needs to hear that she is still working on his request.
    assert len(alerts) == 1 and "月月" in alerts[0]
    assert not any(word in alerts[0] for word in ("診斷", "重啟", "訊息處理")), alerts[0]
    watchdog.beat("tg_get_updates")
    assert watchdog.check_once() == [], "warn must not repeat for the same turn"
    watchdog.end_turn(token)
    clock.advance(9999.0)
    watchdog.beat("tg_get_updates")
    assert watchdog.check_once() == [], "ended turn must not trigger anything"
    assert not exits


def check_turn_exit_restarts(tmp_dir: str) -> None:
    watchdog, clock, alerts, exits = build(tmp_dir)
    watchdog.beat("tg_get_updates")
    watchdog.begin_turn(123, 789)
    clock.advance(601.0)
    watchdog.beat("tg_get_updates")
    incidents = watchdog.check_once()
    assert incidents == ["turn_stuck_exit"], incidents
    assert exits == [RESTART_EXIT_CODE]
    assert any("重啟" in text for text in alerts)
    dumps = list((Path(tmp_dir) / "watchdog").glob("stall_*.log"))
    assert dumps and "in_flight_turn" in dumps[0].read_text(encoding="utf-8")


def check_finished_turn_is_forgotten(tmp_dir: str) -> None:
    watchdog, clock, alerts, exits = build(tmp_dir)
    watchdog.beat("tg_get_updates")
    for index in range(5):
        token = watchdog.begin_turn(1, index)
        clock.advance(30.0)
        watchdog.beat("tg_get_updates")
        watchdog.end_turn(token)
        assert watchdog.check_once() == []
    assert not alerts and not exits


CHECKS = [
    check_healthy_loop_never_fires,
    check_poll_stall_restarts,
    check_no_poll_beat_means_no_restart,
    check_turn_warn_fires_once,
    check_turn_exit_restarts,
    check_finished_turn_is_forgotten,
]


def main() -> int:
    failures = 0
    for check in CHECKS:
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                check(tmp_dir)
                print(f"ok {check.__name__}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {check.__name__}: {exc}")
    if failures:
        print(f"watchdog_check: {failures} failure(s)")
        return 1
    print("watchdog_check: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
