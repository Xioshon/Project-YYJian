"""Silent-stall watchdog for the Telegram gateway.

The previously observed failure mode was: the process stays alive, Telegram
delivers a message, and nothing happens - no handler log, no error, no reply -
until a manual restart. This module makes that failure mode impossible to miss
and recoverable without the owner touching the machine:

1. Heartbeats. The gateway beats on every ``get_updates`` call, so a healthy
   long-polling loop beats at least every ~25 seconds even with zero traffic
   and even while the network is flapping (beats happen on call, not on
   success). A stale heartbeat therefore means the polling thread itself is
   wedged, not that the Wi-Fi dropped.
2. In-flight turn tracking. Every aggregated turn registers before processing
   and deregisters in a ``finally``. A turn stuck past the warn threshold gets
   the owner an honest alert; stuck past the exit threshold triggers a process
   restart.
3. Forensics before recovery. Whenever a stall is detected, every thread's
   stack is dumped to ``workspace/logs/watchdog/`` so the root cause is
   captured *before* the restart destroys the evidence.
4. Recovery. ``start_yueyue.bat`` already restarts the process 5 seconds after
   any exit, so ``os._exit(RESTART_EXIT_CODE)`` is a clean self-heal path.

Owner alerts go through a direct HTTPS call with its own timeout - never
through the possibly-hung TeleBot instance.
"""

from __future__ import annotations

import contextlib
import faulthandler
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agent_hooks import emit_trace

POLL_STALL_SECONDS = 150.0
TURN_WARN_SECONDS = 180.0
TURN_EXIT_SECONDS = 600.0
CHECK_INTERVAL_SECONDS = 15.0
RESTART_EXIT_CODE = 21
ALERT_TIMEOUT_SECONDS = 10.0


class _InFlightTurn:
    __slots__ = ("token", "chat_id", "message_id", "started_at", "warned")

    def __init__(self, token: int, chat_id: str, message_id: str, started_at: float):
        self.token = token
        self.chat_id = chat_id
        self.message_id = message_id
        self.started_at = started_at
        self.warned = False


class GatewayWatchdog:
    """Detects a wedged polling loop or a stuck turn, records forensics, alerts, recovers."""

    def __init__(
        self,
        token: str = "",
        chat_id_reader: Callable[[], str] | None = None,
        dump_dir: str | Path = "workspace/logs/watchdog",
        clock: Callable[[], float] = time.monotonic,
        alert_fn: Callable[[str], bool] | None = None,
        exit_fn: Callable[[int], None] | None = None,
        poll_stall_seconds: float = POLL_STALL_SECONDS,
        turn_warn_seconds: float = TURN_WARN_SECONDS,
        turn_exit_seconds: float = TURN_EXIT_SECONDS,
        check_interval_seconds: float = CHECK_INTERVAL_SECONDS,
    ):
        self._token = token
        self._chat_id_reader = chat_id_reader
        self.dump_dir = Path(dump_dir)
        self._clock = clock
        self._alert_fn = alert_fn
        self._exit_fn = exit_fn or os._exit
        self.poll_stall_seconds = poll_stall_seconds
        self.turn_warn_seconds = turn_warn_seconds
        self.turn_exit_seconds = turn_exit_seconds
        self.check_interval_seconds = check_interval_seconds
        self._lock = threading.Lock()
        self._heartbeats: dict[str, float] = {}
        self._turns: dict[int, _InFlightTurn] = {}
        self._next_token = 1
        self._poll_stall_reported = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- signals from the gateway -------------------------------------------------

    def beat(self, name: str) -> None:
        with self._lock:
            self._heartbeats[name] = self._clock()
            if name == "tg_get_updates":
                self._poll_stall_reported = False

    def begin_turn(self, chat_id: int | str, message_id: int | str) -> int:
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._turns[token] = _InFlightTurn(token, str(chat_id), str(message_id), self._clock())
            return token

    def end_turn(self, token: int) -> None:
        with self._lock:
            self._turns.pop(token, None)

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gateway-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval_seconds):
            try:
                self.check_once()
            except Exception as exc:
                print(f"[watchdog error] {type(exc).__name__}: {exc}")

    # -- detection ------------------------------------------------------------------

    def check_once(self) -> list[str]:
        """Run one detection pass. Returns the list of incidents found (for tests)."""
        now = self._clock()
        incidents: list[str] = []
        with self._lock:
            poll_beat = self._heartbeats.get("tg_get_updates")
            poll_stale = (
                poll_beat is not None
                and now - poll_beat > self.poll_stall_seconds
                and not self._poll_stall_reported
            )
            if poll_stale:
                self._poll_stall_reported = True
            stuck_warn: list[_InFlightTurn] = []
            stuck_exit: list[_InFlightTurn] = []
            for turn in self._turns.values():
                age = now - turn.started_at
                if age > self.turn_exit_seconds:
                    stuck_exit.append(turn)
                elif age > self.turn_warn_seconds and not turn.warned:
                    turn.warned = True
                    stuck_warn.append(turn)

        for turn in stuck_warn:
            age = int(now - turn.started_at)
            incidents.append("turn_stuck_warn")
            dump_path = self._dump_stacks(f"turn_stuck_warn chat={turn.chat_id} message={turn.message_id} age={age}s")
            emit_trace(
                "watchdog.turn_stuck",
                level="warn",
                chat_id=turn.chat_id,
                message_id=turn.message_id,
                age_seconds=age,
                dump_path=str(dump_path or ""),
            )
            self._alert_owner(
                f"喵，我有一則訊息處理卡住超過 {age // 60} 分鐘了，還在嘗試。"
                f"如果一直沒動靜，我會自己重啟恢復。（診斷已存檔）"
            )

        if stuck_exit:
            turn = stuck_exit[0]
            age = int(now - turn.started_at)
            incidents.append("turn_stuck_exit")
            dump_path = self._dump_stacks(f"turn_stuck_exit chat={turn.chat_id} message={turn.message_id} age={age}s")
            emit_trace(
                "watchdog.restart",
                reason="turn_stuck",
                chat_id=turn.chat_id,
                message_id=turn.message_id,
                age_seconds=age,
                dump_path=str(dump_path or ""),
            )
            self._alert_owner(
                f"喵，剛剛那則訊息卡死超過 {age // 60} 分鐘，我現在自動重啟恢復，"
                f"大約 30 秒內回來。診斷堆疊已存檔，之後可以查原因。"
            )
            self._request_restart()
            return incidents

        if poll_stale:
            age = int(now - (poll_beat or now))
            incidents.append("poll_stalled")
            dump_path = self._dump_stacks(f"poll_stalled age={age}s")
            emit_trace("watchdog.restart", reason="poll_stalled", age_seconds=age, dump_path=str(dump_path or ""))
            self._alert_owner(
                "喵，我的訊息接收通道卡住了（不是斷網，是線程卡死），"
                "我現在自動重啟恢復，大約 30 秒內回來。"
            )
            self._request_restart()

        return incidents

    # -- forensics / alerting / recovery ---------------------------------------------

    def _dump_stacks(self, reason: str) -> Path | None:
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            path = self.dump_dir / f"stall_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
            with open(path, "w", encoding="utf-8") as file:
                file.write(f"reason: {reason}\n")
                file.write(f"wall_time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                with self._lock:
                    for turn in self._turns.values():
                        file.write(
                            f"in_flight_turn: chat={turn.chat_id} message={turn.message_id} "
                            f"age={self._clock() - turn.started_at:.0f}s\n"
                        )
                file.write("\n=== all thread stacks ===\n")
                file.flush()
                faulthandler.dump_traceback(file=file, all_threads=True)
            print(f"[watchdog] stall forensics written: {path}")
            return path
        except Exception as exc:
            print(f"[watchdog] stack dump failed: {type(exc).__name__}: {exc}")
            return None

    def _alert_owner(self, text: str) -> bool:
        if self._alert_fn:
            try:
                return bool(self._alert_fn(text))
            except Exception:
                return False
        if not self._token or not self._chat_id_reader:
            return False
        try:
            chat_id = str(self._chat_id_reader() or "").strip()
            if not chat_id:
                return False
            import requests

            response = requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=ALERT_TIMEOUT_SECONDS,
            )
            return bool(response.ok)
        except Exception as exc:
            print(f"[watchdog] owner alert failed: {type(exc).__name__}: {exc}")
            return False

    def _request_restart(self) -> None:
        print(f"[watchdog] requesting process restart (exit code {RESTART_EXIT_CODE})")
        with contextlib.suppress(Exception):
            import sys

            sys.stdout.flush()
            sys.stderr.flush()
        self._exit_fn(RESTART_EXIT_CODE)


def read_chat_id_file(path: str | Path) -> str:
    try:
        with open(path, encoding="utf-8") as file:
            return file.read().strip()
    except OSError:
        return ""
