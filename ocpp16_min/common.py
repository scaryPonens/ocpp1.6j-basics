from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def make_call(uid: str, action: str, payload: dict[str, Any]) -> list[Any]:
    return [2, uid, action, payload]


def make_call_result(uid: str, payload: dict[str, Any]) -> list[Any]:
    return [3, uid, payload]


def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_message(text: str) -> Any:
    return json.loads(text)
