"""Privacy-preserving usage events for the hosted browser application."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading


EVENT_NAME = "adoc_dita_usage"
WORKFLOWS = ("convert", "compare", "pull_request")


def anonymous_user(visitor_id):
    """Return a stable, non-reversible label for a random browser identifier."""
    return hashlib.sha256(visitor_id.encode("utf-8")).hexdigest()[:24]


class UsageLogger:
    """Write one small JSON event per completed or failed workflow use."""

    def __init__(self, path=None, stream=None):
        self.path = Path(path).expanduser() if path else None
        self.stream = stream
        self.lock = threading.Lock()
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                # Telemetry must never prevent the converter from starting.
                self.path = None

    def record(self, workflow, outcome, visitor_id, duration_ms, **counts):
        if workflow not in WORKFLOWS:
            raise ValueError(f"Unknown usage workflow: {workflow}")
        event = {
            "event": EVENT_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "workflow": workflow,
            "outcome": str(outcome),
            "anonymous_user": anonymous_user(visitor_id),
            "duration_ms": max(0, int(duration_ms)),
        }
        for name, value in sorted(counts.items()):
            if isinstance(value, bool):
                event[name] = value
            elif isinstance(value, int):
                event[name] = max(0, value)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.lock:
            if self.stream:
                try:
                    print(line, file=self.stream, flush=True)
                except OSError:
                    pass
            if self.path:
                try:
                    with self.path.open("a", encoding="utf-8") as output:
                        output.write(line + "\n")
                except OSError:
                    pass
        return event


def summarize_usage(lines):
    """Aggregate application events while ignoring unrelated log lines."""
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if event.get("event") == EVENT_NAME and event.get("workflow") in WORKFLOWS:
            events.append(event)
    summary = {
        "uses": len(events),
        "anonymous_users": len({event.get("anonymous_user") for event in events
                                if event.get("anonymous_user")}),
        "workflows": {},
    }
    for workflow in WORKFLOWS:
        selected = [event for event in events if event["workflow"] == workflow]
        outcomes = defaultdict(int)
        for event in selected:
            outcomes[str(event.get("outcome", "unknown"))] += 1
        summary["workflows"][workflow] = {
            "uses": len(selected),
            "anonymous_users": len({event.get("anonymous_user") for event in selected
                                    if event.get("anonymous_user")}),
            "outcomes": dict(sorted(outcomes.items())),
        }
    timestamps = [event["timestamp"] for event in events if event.get("timestamp")]
    if timestamps:
        summary["first_event"] = min(timestamps)
        summary["last_event"] = max(timestamps)
    else:
        summary["first_event"] = summary["last_event"] = None
    return summary


def summarize_usage_file(path):
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"Usage log not found: {path}")
    with path.open(encoding="utf-8", errors="replace") as source:
        return summarize_usage(source)
