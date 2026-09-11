"""Functional test for task_logger.py (offline, stdlib only).

Run:  .venv\\Scripts\\python.exe _task_logger_smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

import task_logger

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 0) Clean slate.
if task_logger.LOG_PATH.exists():
    task_logger.LOG_PATH.unlink()
assert task_logger.get_action_log() == [], "missing file must read as []"
print("PASS 0 missing file -> []")

# 1) Log a sent action.
rec1 = task_logger.log_action(
    "sent",
    "Q3 Budget Review — sign-off needed by Thursday",
    "meera.iyer@acme.com",
    "1948f2c0a9d3e11b",
)
assert rec1["action_type"] == "sent" and rec1["detail"] == "meera.iyer@acme.com"
assert rec1["id"] == "1948f2c0a9d3e11b" and rec1["thread_subject"].startswith("Q3")
assert rec1["timestamp"], rec1
print("PASS 1 log_action('sent') -> record with timestamp/type/subject/detail/id")

# 2) Log a booked action; both must persist (file auto-created/preserved).
rec2 = task_logger.log_action(
    "booked",
    "Design review: new onboarding flow — Thu Sep 10?",
    "Design review: new onboarding flow",
    "stub-event-001",
)
log = task_logger.get_action_log()
assert len(log) == 2 and log[0] == rec1 and log[1] == rec2, log
assert task_logger.LOG_PATH.exists()
print("PASS 2 second log_action appends; get_action_log returns both in order")

# 3) Invalid action_type must raise, not corrupt the log.
try:
    task_logger.log_action("emailed", "s", "d", "i")  # type: ignore[arg-type]
except ValueError:
    assert len(task_logger.get_action_log()) == 2, "failed call must not write"
    print("PASS 3 invalid action_type raises ValueError, log untouched")
else:
    raise AssertionError("invalid action_type did not raise")

# 4) clear_log resets to an empty list that reads back as [].
task_logger.clear_log()
assert task_logger.LOG_PATH.read_text(encoding="utf-8") == "[]"
assert task_logger.get_action_log() == [], "cleared log must read as []"
print("PASS 4 clear_log -> file contains [] and reads as []")

# 5) Empty-but-present file reads as [] (spec: empty counts as no entries).
task_logger.LOG_PATH.write_text("", encoding="utf-8")
assert task_logger.get_action_log() == [], "empty file must read as []"
print("PASS 5 empty file -> []")

# 6) Tidy up: remove the test artifact so real usage starts fresh.
task_logger.LOG_PATH.unlink()
assert not Path("action_log.json").exists()
print("PASS 6 cleanup: test action_log.json removed")
print()
print("All task_logger smoke checks passed.")
