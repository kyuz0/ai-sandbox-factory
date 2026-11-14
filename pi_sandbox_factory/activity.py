"""Recorded lifecycle times, kept separate from image/configuration identities."""
from datetime import datetime, timezone
import json
from pathlib import Path

from .sandbox_options import write_json

FIELDS = ('created', 'edited', 'started')


def record(state, event):
    if event not in FIELDS:
        raise ValueError('Unknown sandbox lifecycle event')
    # Independent files prevent concurrent starts and settings saves losing events.
    write_json(Path(state) / 'activity' / (event + '.json'),
               datetime.now(timezone.utc).isoformat())


def load(state):
    result = {}
    for event in FIELDS:
        try:
            value = json.loads((Path(state) / 'activity' / (event + '.json')).read_text())
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError('Timestamp requires a timezone')
            result[event] = parsed.astimezone(timezone.utc).isoformat()
        except (OSError, ValueError, TypeError):
            result[event] = None
    return result


def display(value):
    return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M') if value else '—'


def save_config(state, value):
    path = Path(state) / 'config.json'
    previous = json.loads(path.read_text()) if path.exists() else None
    write_json(path, value)
    if previous is not None and previous != value:
        record(state, 'edited')
