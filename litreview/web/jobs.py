"""Runs work queued from the web UI in two lanes, each one process at a time: reviews (each in its
own `litreview resume` process) and paper tasks (a `litreview tasks` process that works through
every queued fetch and summary). Tasks don't wait behind an hour-long review.

A review's state lives in the library (queued -> running -> paused / done / failed), and its
progress lines in run_events, so this class holds nothing that matters: if the server restarts,
the review its process was running is marked paused and can be continued. Pausing sends the
process Ctrl-C, which the pipeline turns into a clean pause.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..library import Library
from ..papers import TASKS_LOCK
from ..pipeline import resolve_folder, run_is_active

INTERRUPTED = "interrupted: the process running it stopped before it finished"


def default_command(db_path: Path):
    return lambda run_id: [sys.executable, "-m", "litreview", "--db", str(db_path), "resume", run_id]


def default_task_command(db_path: Path):
    return lambda: [sys.executable, "-m", "litreview", "--db", str(db_path), "tasks"]


class JobRunner:
    def __init__(self, db_path: Path | str, runs_dir: Path, command=None, poll: float = 1.0, task_command=None):
        self.db_path = Path(db_path)
        self.runs_dir = runs_dir
        self.command = command or default_command(self.db_path)
        self.task_command = task_command or default_task_command(self.db_path)
        self.poll = poll
        self.task_proc: subprocess.Popen | None = None
        self.proc: subprocess.Popen | None = None
        self.current: str | None = None
        self.started_at: float | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="litreview-jobs", daemon=True)

    # --- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        self.recover()
        self._thread.start()

    def stop(self, wait: float = 15.0) -> None:
        """Stop taking work; a review in progress is paused, so it can be continued later."""
        self._stop.set()
        with self._lock:
            procs = [p for p in (self.proc, self.task_proc) if p]
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in procs:
            try:
                proc.wait(wait)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._thread.join(timeout=wait)

    def _lib(self) -> Library:
        return Library(self.db_path, schema=False, check_same_thread=False)

    def recover(self) -> None:
        """Runs marked running that no process holds are left over from a stopped process."""
        lib = self._lib()
        try:
            for r in lib.db.execute("SELECT run_id FROM runs WHERE status = 'running'").fetchall():
                if r["run_id"] != self.current and not run_is_active(lib, r["run_id"]):
                    lib.set_run(r["run_id"], status="paused", error=INTERRUPTED)
            if not run_is_active(lib, TASKS_LOCK):
                for t in lib.db.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall():
                    lib.finish_task(t["id"], "failed", INTERRUPTED)
        finally:
            lib.close()

    # --- the loop -----------------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # keep serving even if one tick fails
                print(f"litreview jobs: {e}", file=sys.stderr)
            self._stop.wait(self.poll)

    def _tick(self) -> None:
        self._tick_tasks()
        self._tick_reviews()

    def _tick_tasks(self) -> None:
        with self._lock:
            if self.task_proc is not None:
                if self.task_proc.poll() is None:
                    return
                self.task_proc = None
                self.recover()
        lib = self._lib()
        try:
            if run_is_active(lib, TASKS_LOCK):  # e.g. `litreview tasks` from the command line
                return
            if lib.db.execute("SELECT 1 FROM tasks WHERE status = 'queued' LIMIT 1").fetchone() is None:
                return
        finally:
            lib.close()
        log = self.db_path.parent / "tasks.log"
        with open(log, "ab") as out:
            proc = subprocess.Popen(self.task_command(), stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        with self._lock:
            self.task_proc = proc

    def _tick_reviews(self) -> None:
        finished, code, started = None, None, None
        with self._lock:
            if self.proc is not None:
                code = self.proc.poll()
                if code is None:
                    return
                finished, started = self.current, self.started_at
                self.proc, self.current, self.started_at = None, None, None
        if finished is not None:
            self._after(finished, code, started)
        lib = self._lib()
        try:
            # One review at a time, including any the CLI is running.
            if any(run_is_active(lib, r["run_id"]) for r in
                   lib.db.execute("SELECT run_id FROM runs WHERE status = 'running'").fetchall()):
                return
            row = lib.db.execute(
                "SELECT run_id, folder FROM runs WHERE status = 'queued' ORDER BY updated_at LIMIT 1"
            ).fetchone()
            if row is None:
                return
            folder = resolve_folder(row["folder"], self.runs_dir)
        finally:
            lib.close()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "worker.log", "ab") as out:
            proc = subprocess.Popen(self.command(row["run_id"]), stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        with self._lock:
            self.proc, self.current, self.started_at = proc, row["run_id"], time.time()

    def _after(self, run_id: str, code: int, started: float) -> None:
        """Tidy up after a worker exits."""
        lib = self._lib()
        try:
            row = lib.run(run_id)
            # Still queued and untouched since the worker started: it never got going. (Queued again
            # since then means it was paused and continued before this tick noticed the exit.)
            if row is not None and row["status"] == "queued" and row["updated_at"] <= started:
                # It never got as far as starting; don't launch it again and again.
                lib.set_run(run_id, status="failed",
                            error=f"the worker exited with code {code} before the run started; see worker.log in its folder")
        finally:
            lib.close()
        self.recover()  # a worker that died mid-run leaves it marked running

    # --- control ------------------------------------------------------------------------------

    def interrupt(self, run_id: str) -> bool:
        """Pause the review this runner is running; False if it isn't running here."""
        with self._lock:
            if self.current != run_id or self.proc is None or self.proc.poll() is not None:
                return False
            self.proc.send_signal(signal.SIGINT)
            return True

    def state(self) -> dict:
        with self._lock:
            return {"current": self.current, "started_at": self.started_at,
                    "tasks": self.task_proc is not None and self.task_proc.poll() is None}
