"""Long-running work started from the web page, such as fetching and processing listings.

The page starts a task with POST /api/tasks and gets an id straight back. The work runs in the
background while this module keeps its row in the tasks table up to date, and the page polls
GET /api/tasks/{id} to follow it.

While a task runs it also has live state, kept in memory only (it's gone after a restart):
  - a log of what it's doing, line by line, like the scripts print (GET /api/tasks/{id}/log)
  - each step's status, for the page's step list
  - a question waiting for an answer: a paid step asks before it spends (POST .../approval)

This module only records status. The work itself lives in each step's module (e.g. ingest.run),
so the command-line scripts and the page share it. Money spent is still recorded in the runs
table by spend.Run, as it is for the scripts.
"""

import json
import sqlite3
import threading
import traceback
from collections import deque
from contextlib import closing
from datetime import datetime

import db

_lock = threading.Lock()  # makes "is one running? if not, start one" a single step
UNFINISHED = ("queued", "running", "waiting")
APPROVAL_TIMEOUT_S = 60 * 60  # a question left unanswered this long counts as "stop"
LOG_LINES = 2000              # the log keeps only the latest lines


class AlreadyRunning(Exception):
    def __init__(self, task_id):
        super().__init__(f"task {task_id} is still running")
        self.task_id = task_id


def now():
    return datetime.now().isoformat(timespec="seconds")


def _write(task_id, **fields):
    """Update a task's row. Opens its own connection, so any thread can call it."""
    sets = ", ".join(f"{name} = ?" for name in fields)
    with _lock, closing(sqlite3.connect(db.DB_PATH, timeout=30)) as conn:
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
        conn.commit()


class Live:
    """A running task's log, steps and pending question. Passed to the work as `task`."""

    def __init__(self, task_id):
        self.task_id = task_id
        self.lines = deque(maxlen=LOG_LINES)
        self.total = 0                 # lines ever logged, so the page can ask for only new ones
        self.steps = []                # [{key, label, status, detail}]
        self.approval = None           # the question waiting for an answer
        self._answer = None
        self._answered = threading.Event()
        self._abort = threading.Event()
        self._guard = threading.Lock()

    def log(self, line):
        with self._guard:
            for part in str(line).splitlines() or [""]:
                self.lines.append(part)
                self.total += 1

    def lines_after(self, after):
        with self._guard:
            first = self.total - len(self.lines)  # number of the oldest line still kept
            start = max(after, first)
            return list(self.lines)[start - first:], self.total

    def progress(self, message):
        """The one-line status shown under the spinner (also saved with the task)."""
        _write(self.task_id, progress=message)

    def plan_steps(self, steps):
        """steps: [(key, label)], all pending to begin with."""
        self.steps = [{"key": k, "label": label, "status": "pending", "detail": None} for k, label in steps]

    def step(self, key, status, detail=None):
        """status: pending / running / waiting / done / skipped / stopped / failed"""
        for s in self.steps:
            if s["key"] == key:
                s["status"], s["detail"] = status, detail

    def ask(self, question):
        """Wait for the page to approve or stop. Returns True to go ahead."""
        if self.aborted():
            return False
        self._answered.clear()
        self._answer = None
        self.approval = question
        _write(self.task_id, status="waiting")
        answered = self._answered.wait(APPROVAL_TIMEOUT_S)
        self.approval = None
        _write(self.task_id, status="running")
        return bool(answered and self._answer and not self.aborted())

    def abort(self):
        """Ask the work to stop: the current job finishes, then it stops. A question waiting for
        an answer counts as Stop."""
        self._abort.set()
        if self.approval is not None:
            self.answer(False)

    def aborted(self):
        return self._abort.is_set()

    def answer(self, approve):
        if self.approval is None:
            return False
        self._answer = approve
        self._answered.set()
        return True


_live: dict[int, Live] = {}  # running tasks only


def live(task_id):
    return _live.get(task_id)


def create(conn, kind, params):
    """Save a queued task and return its id. Only one task of each kind runs at a time."""
    with _lock:
        busy = conn.execute(f"SELECT id FROM tasks WHERE kind = ? AND status IN ({','.join('?' * len(UNFINISHED))})",
                            (kind, *UNFINISHED)).fetchone()
        if busy:
            raise AlreadyRunning(busy["id"])
        task_id = conn.execute(
            "INSERT INTO tasks (kind, params, status, created_at) VALUES (?, ?, 'queued', ?)",
            (kind, json.dumps(params), now())).lastrowid
        conn.commit()
        _live.clear()  # one task runs at a time, so older logs can go
        _live[task_id] = Live(task_id)
    return task_id


def execute(task_id, work):
    """Run work(conn, task) and record how it went. work returns a JSON-safe result.
    Called in a background thread, so it opens its own database connection."""
    conn = db.connect()
    task = _live.setdefault(task_id, Live(task_id))
    _write(task_id, status="running", started_at=now())
    try:
        result = work(conn, task)
        _write(task_id, status="done", result=json.dumps(result), finished_at=now())
    except Exception as e:
        traceback.print_exc()  # full detail in the uvicorn terminal
        task.log(f"FAILED: {type(e).__name__}: {e}")
        _write(task_id, status="failed", error=f"{type(e).__name__}: {e}", finished_at=now())
    finally:
        conn.close()
        # The log stays readable until the next task starts; the steps are saved with the result.


def fail_unfinished(conn):
    """Mark tasks left running by a server that stopped (a restart, or --reload after an edit).
    Called once when the API starts."""
    conn.execute(f"""UPDATE tasks SET status = 'failed', finished_at = ?,
                         error = 'The API stopped before this task finished'
                     WHERE status IN ({','.join('?' * len(UNFINISHED))})""", (now(), *UNFINISHED))
    conn.commit()


def _row(row):
    task = dict(row)
    for name in ("params", "result"):
        task[name] = json.loads(task[name]) if task[name] else None
    t = _live.get(task["id"])
    task["steps"] = t.steps if t else (task["result"] or {}).get("steps", [])
    task["approval"] = t.approval if t else None
    task["abort_requested"] = t.aborted() if t else False
    task["log_lines"] = t.total if t else 0
    return task


def get(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row(row) if row else None


def recent(conn, limit=20):
    return [_row(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))]
