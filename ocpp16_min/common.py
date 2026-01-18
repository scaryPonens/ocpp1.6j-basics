from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

BOOTING = "BOOTING"
AVAILABLE = "AVAILABLE"


def make_call(uid: str, action: str, payload: dict[str, Any]) -> list[Any]:
    return [2, uid, action, payload]


def make_call_result(uid: str, payload: dict[str, Any]) -> list[Any]:
    return [3, uid, payload]


def make_call_error(uid: str, code: str, description: str, details: dict[str, Any] | None = None) -> list[Any]:
    return [4, uid, code, description, details or {}]


def new_uid() -> str:
    return uuid4().hex


def is_call(msg: Any) -> bool:
    return isinstance(msg, list) and len(msg) == 4 and msg[0] == 2


def is_call_result(msg: Any) -> bool:
    return isinstance(msg, list) and len(msg) >= 3 and msg[0] == 3


def validate_call(msg: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(msg, list):
        raise ValueError("frame must be a JSON list")
    if len(msg) != 4:
        raise ValueError("CALL frame must have length 4")
    message_type, uid, action, payload = msg
    if message_type != 2:
        raise ValueError("MessageTypeId must be 2 (CALL)")
    if not isinstance(uid, str) or not uid:
        raise ValueError("CALL uid must be a non-empty string")
    if not isinstance(action, str) or not action:
        raise ValueError("CALL action must be a non-empty string")
    if not isinstance(payload, dict):
        raise ValueError("CALL payload must be an object")
    return uid, action, payload


def make_heartbeat_call(uid: str | None = None) -> list[Any]:
    return make_call(uid or new_uid(), "Heartbeat", {})


def make_status_notification_call(
    uid: str | None = None,
    connector_id: int = 0,
    status: str = "Available",
    error_code: str = "NoError",
) -> list[Any]:
    payload = {
        "connectorId": connector_id,
        "status": status,
        "errorCode": error_code,
        "timestamp": utc_now_iso_z(),
    }
    return make_call(uid or new_uid(), "StatusNotification", payload)


def make_start_transaction_call(
    uid: str | None = None,
    connector_id: int = 1,
    id_tag: str = "TEST",
    meter_start: int = 0,
    timestamp: str | None = None,
) -> list[Any]:
    payload = {
        "connectorId": connector_id,
        "idTag": id_tag,
        "meterStart": meter_start,
        "timestamp": timestamp or utc_now_iso_z(),
    }
    return make_call(uid or new_uid(), "StartTransaction", payload)


def make_stop_transaction_call(
    uid: str | None = None,
    transaction_id: int = 0,
    id_tag: str = "TEST",
    meter_stop: int = 42,
    timestamp: str | None = None,
    reason: str = "Local",
) -> list[Any]:
    payload = {
        "transactionId": transaction_id,
        "meterStop": meter_stop,
        "timestamp": timestamp or utc_now_iso_z(),
        "idTag": id_tag,
        "reason": reason,
    }
    return make_call(uid or new_uid(), "StopTransaction", payload)


def make_meter_values_call(
    uid: str | None = None,
    connector_id: int = 1,
    transaction_id: int | None = None,
    energy_wh: int = 0,
    timestamp: str | None = None,
) -> list[Any]:
    entry = {
        "timestamp": timestamp or utc_now_iso_z(),
        "sampledValue": [
            {
                "value": str(energy_wh),
                "measurand": "Energy.Active.Import.Register",
                "unit": "Wh",
            }
        ],
    }
    payload: dict[str, Any] = {
        "connectorId": connector_id,
        "meterValue": [entry],
    }
    if transaction_id is not None:
        payload["transactionId"] = transaction_id
    return make_call(uid or new_uid(), "MeterValues", payload)


def make_set_charging_profile_call(
    uid: str | None = None,
    connector_id: int = 1,
    profile_id: int = 1,
    limit_kw: float = 7.0,
) -> list[Any]:
    limit_w = int(limit_kw * 1000)
    charging_profile = {
        "chargingProfileId": profile_id,
        "stackLevel": 1,
        "chargingProfilePurpose": "TxProfile",
        "chargingProfileKind": "Absolute",
        "chargingSchedule": {
            "chargingRateUnit": "W",
            "chargingSchedulePeriod": [
                {
                    "startPeriod": 0,
                    "limit": limit_w,
                }
            ],
        },
    }
    payload = {
        "connectorId": connector_id,
        "chargingProfile": charging_profile,
    }
    return make_call(uid or new_uid(), "SetChargingProfile", payload)


def make_clear_charging_profile_call(uid: str | None = None, profile_id: int | None = None) -> list[Any]:
    payload: dict[str, Any] = {}
    if profile_id is not None:
        payload["chargingProfileId"] = profile_id
    return make_call(uid or new_uid(), "ClearChargingProfile", payload)


def is_set_charging_profile(action: str) -> bool:
    return action == "SetChargingProfile"


def is_clear_charging_profile(action: str) -> bool:
    return action == "ClearChargingProfile"


def get_charging_profile_id(payload: dict[str, Any]) -> int | None:
    if not isinstance(payload, dict):
        return None
    if "chargingProfileId" in payload:
        value = payload.get("chargingProfileId")
    else:
        profile = payload.get("chargingProfile")
        if isinstance(profile, dict):
            value = profile.get("chargingProfileId")
        else:
            value = None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_message(text: str) -> Any:
    return json.loads(text)


def parse_call_result_payload(msg: Any) -> dict[str, Any]:
    if not isinstance(msg, list) or len(msg) < 3 or msg[0] != 3:
        raise ValueError("CALLRESULT frame expected")
    payload = msg[2]
    if not isinstance(payload, dict):
        raise ValueError("CALLRESULT payload must be an object")
    return payload


def coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def parse_iso_z(text: Any, field_name: str) -> datetime:
    if not isinstance(text, str) or not text:
        raise ValueError(f"{field_name} must be a string")
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc


@dataclass
class SessionState:
    transaction_id: int
    connector_id: int
    meter_start: int
    meter_stop: int
