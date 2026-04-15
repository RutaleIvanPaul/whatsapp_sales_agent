import json
from datetime import datetime, timezone


def log(event: str, **kwargs) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    print(json.dumps(entry, default=str))
