"""Timestamped progress lines on stderr, and appended to $JOBAGENT_LOG when set (the n8n
command sets it, so a long run can be watched with `tail -f logs/jobagent.log`)."""
import os
import sys
from datetime import datetime


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    path = os.environ.get("JOBAGENT_LOG")
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
