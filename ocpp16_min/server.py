from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import websockets
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from websockets.exceptions import ConnectionClosed

try:
    from .common import (
        coerce_int,
        get_charging_profile_id,
        make_call_error,
        make_call_result,
        parse_iso_z,
        parse_message,
        utc_now_iso_z,
        validate_call,
    )
except ImportError:  # Allows running as a script without -m
    from common import (
        coerce_int,
        get_charging_profile_id,
        make_call_error,
        make_call_result,
        parse_iso_z,
        parse_message,
        utc_now_iso_z,
        validate_call,
    )

CALL = 2
CALL_RESULT = 3
CALL_ERROR = 4
HEARTBEAT_INTERVAL_SECONDS = 10

_next_transaction_id = 1


@dataclass
class ChargePointSession:
    connected: bool = False
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    last_seen_at: datetime | None = None
    boot_accepted: bool = False
    boot_info: dict | None = None
    last_heartbeat_at: datetime | None = None
    status: str | None = None
    connector_id: int | None = None
    active_transaction_id: int | None = None
    transactions: dict[int, dict] = field(default_factory=dict)
    last_meter_wh: int | None = None
    charging_profiles: dict[int, dict] = field(default_factory=dict)


logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
raw_logger = logging.getLogger("ocpp.raw")
tracer = trace.get_tracer(__name__)

_sessions: dict[str, ChargePointSession] = {}


def setup_tracing() -> None:
    service_name = os.getenv("OTEL_SERVICE_NAME", "ocpp16-server")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)


def setup_raw_logging() -> None:
    if raw_logger.handlers:
        return
    os.makedirs("logs", exist_ok=True)
    handler = RotatingFileHandler("logs/ocpp_raw.log", maxBytes=1_000_000, backupCount=3)
    formatter = logging.Formatter("%(asctime)s %(message)s")
    handler.setFormatter(formatter)
    raw_logger.setLevel(logging.INFO)
    raw_logger.addHandler(handler)
    raw_logger.propagate = False


class SpanEventHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        span = trace.get_current_span()
        if span and span.is_recording():
            span.add_event(
                "log",
                {
                    "log.level": record.levelname,
                    "log.message": record.getMessage(),
                    "log.logger": record.name,
                },
            )


class ValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_session(charge_point_id: str) -> ChargePointSession:
    session = _sessions.get(charge_point_id)
    if session is None:
        session = ChargePointSession()
        _sessions[charge_point_id] = session
    return session


def _raw_log(direction: str, charge_point_id: str, text: str) -> None:
    raw_logger.info("%s %s %s", charge_point_id, direction, text)


def _compact_log(
    charge_point_id: str,
    direction: str,
    action: str,
    uid: str,
    summary: dict[str, object] | None = None,
) -> None:
    ts = utc_now_iso_z()
    compact = summary or {}
    logger.info(
        "%s cp=%s dir=%s action=%s uid=%s summary=%s",
        ts,
        charge_point_id,
        direction,
        action,
        uid,
        compact,
    )


def _summary_for(action: str, payload: dict[str, object]) -> dict[str, object]:
    if action == "BootNotification":
        return {
            "vendor": payload.get("chargePointVendor"),
            "model": payload.get("chargePointModel"),
        }
    if action == "Heartbeat":
        return {}
    if action == "StatusNotification":
        return {
            "connectorId": payload.get("connectorId"),
            "status": payload.get("status"),
            "errorCode": payload.get("errorCode"),
        }
    if action == "StartTransaction":
        return {
            "connectorId": payload.get("connectorId"),
            "idTag": payload.get("idTag"),
            "meterStart": payload.get("meterStart"),
        }
    if action == "StopTransaction":
        return {
            "transactionId": payload.get("transactionId"),
            "meterStop": payload.get("meterStop"),
        }
    if action == "MeterValues":
        return {
            "connectorId": payload.get("connectorId"),
            "transactionId": payload.get("transactionId"),
        }
    if action == "SetChargingProfile":
        profile = payload.get("chargingProfile")
        schedule = profile.get("chargingSchedule") if isinstance(profile, dict) else {}
        periods = schedule.get("chargingSchedulePeriod") if isinstance(schedule, dict) else []
        first = periods[0] if isinstance(periods, list) and periods else {}
        return {
            "profileId": get_charging_profile_id(payload),
            "stackLevel": profile.get("stackLevel") if isinstance(profile, dict) else None,
            "purpose": profile.get("chargingProfilePurpose") if isinstance(profile, dict) else None,
            "limit": first.get("limit") if isinstance(first, dict) else None,
        }
    if action == "ClearChargingProfile":
        return {"profileId": get_charging_profile_id(payload)}
    return {}


def _validate_payload(action: str, payload: dict[str, object]) -> None:
    if action == "BootNotification":
        if not isinstance(payload.get("chargePointVendor"), str):
            raise ValidationError("PropertyConstraintViolation", "chargePointVendor must be a string")
        if not isinstance(payload.get("chargePointModel"), str):
            raise ValidationError("PropertyConstraintViolation", "chargePointModel must be a string")
        return

    if action == "Heartbeat":
        return

    if action == "StatusNotification":
        connector_id = payload.get("connectorId")
        status = payload.get("status")
        error_code = payload.get("errorCode")
        if connector_id not in (0, 1):
            raise ValidationError("PropertyConstraintViolation", "connectorId must be 0 or 1")
        if status != "Available":
            raise ValidationError("PropertyConstraintViolation", "status must be Available")
        if error_code != "NoError":
            raise ValidationError("PropertyConstraintViolation", "errorCode must be NoError")
        return

    if action == "StartTransaction":
        connector_id = payload.get("connectorId")
        if connector_id not in (0, 1):
            raise ValidationError("PropertyConstraintViolation", "connectorId must be 0 or 1")
        if not isinstance(payload.get("idTag"), str) or not payload.get("idTag"):
            raise ValidationError("PropertyConstraintViolation", "idTag must be a non-empty string")
        coerce_int(payload.get("meterStart"), "meterStart")
        parse_iso_z(payload.get("timestamp"), "timestamp")
        return

    if action == "StopTransaction":
        coerce_int(payload.get("transactionId"), "transactionId")
        coerce_int(payload.get("meterStop"), "meterStop")
        parse_iso_z(payload.get("timestamp"), "timestamp")
        return

    if action == "MeterValues":
        connector_id = payload.get("connectorId")
        meter_value = payload.get("meterValue")
        if connector_id not in (0, 1):
            raise ValidationError("PropertyConstraintViolation", "connectorId must be 0 or 1")
        if not isinstance(meter_value, list) or not meter_value:
            raise ValidationError("PropertyConstraintViolation", "meterValue must be a non-empty list")
        first = meter_value[0]
        if not isinstance(first, dict):
            raise ValidationError("PropertyConstraintViolation", "meterValue entry must be an object")
        parse_iso_z(first.get("timestamp"), "timestamp")
        sampled = first.get("sampledValue")
        if not isinstance(sampled, list) or not sampled:
            raise ValidationError("PropertyConstraintViolation", "sampledValue must be a non-empty list")
        first_sample = sampled[0]
        if not isinstance(first_sample, dict):
            raise ValidationError("PropertyConstraintViolation", "sampledValue entry must be an object")
        if "value" not in first_sample:
            raise ValidationError("PropertyConstraintViolation", "sampledValue.value is required")
        return

    if action == "SetChargingProfile":
        charging_profile = payload.get("chargingProfile")
        if not isinstance(charging_profile, dict):
            raise ValidationError("PropertyConstraintViolation", "chargingProfile must be an object")
        coerce_int(charging_profile.get("chargingProfileId"), "chargingProfileId")
        coerce_int(charging_profile.get("stackLevel"), "stackLevel")
        if not isinstance(charging_profile.get("chargingProfilePurpose"), str):
            raise ValidationError("PropertyConstraintViolation", "chargingProfilePurpose must be a string")
        if not isinstance(charging_profile.get("chargingProfileKind"), str):
            raise ValidationError("PropertyConstraintViolation", "chargingProfileKind must be a string")
        schedule = charging_profile.get("chargingSchedule")
        if not isinstance(schedule, dict):
            raise ValidationError("PropertyConstraintViolation", "chargingSchedule must be an object")
        if not isinstance(schedule.get("chargingRateUnit"), str):
            raise ValidationError("PropertyConstraintViolation", "chargingRateUnit must be a string")
        periods = schedule.get("chargingSchedulePeriod")
        if not isinstance(periods, list) or not periods:
            raise ValidationError("PropertyConstraintViolation", "chargingSchedulePeriod must be a non-empty list")
        first = periods[0]
        if not isinstance(first, dict):
            raise ValidationError("PropertyConstraintViolation", "chargingSchedulePeriod entry must be an object")
        if "limit" not in first:
            raise ValidationError("PropertyConstraintViolation", "chargingSchedulePeriod.limit is required")
        if not isinstance(first.get("limit"), (int, float)):
            raise ValidationError("PropertyConstraintViolation", "chargingSchedulePeriod.limit must be a number")
        return

    if action == "ClearChargingProfile":
        profile_id = payload.get("chargingProfileId")
        if profile_id is not None:
            coerce_int(profile_id, "chargingProfileId")
        return

    raise ValidationError("PropertyConstraintViolation", "unsupported action")


def _send_call_error(
    websocket: websockets.WebSocketServerProtocol,
    charge_point_id: str,
    uid: str,
    code: str,
    description: str,
    action: str,
) -> asyncio.Task:
    frame = make_call_error(uid, code, description)
    text = json.dumps(frame)
    _compact_log(charge_point_id, "TX", action, uid, {"error": code, "message": description})
    _raw_log("TX", charge_point_id, text)
    return asyncio.create_task(websocket.send(text))


async def _send_call_result(
    websocket: websockets.WebSocketServerProtocol,
    charge_point_id: str,
    uid: str,
    action: str,
    payload: dict[str, object],
) -> None:
    frame = make_call_result(uid, payload)
    text = json.dumps(frame)
    _compact_log(charge_point_id, "TX", action, uid, {"result": True})
    _raw_log("TX", charge_point_id, text)
    await websocket.send(text)


def _dump_state_summary() -> dict[str, object]:
    items = []
    for cp_id, session in _sessions.items():
        profiles = []
        for profile_id, profile_data in session.charging_profiles.items():
            profiles.append(
                {
                    "id": profile_id,
                    "limit_w": profile_data.get("limit_w"),
                }
            )
        items.append(
            {
                "chargePointId": cp_id,
                "boot_accepted": session.boot_accepted,
                "active_transaction_id": session.active_transaction_id,
                "last_seen_at": session.last_seen_at.isoformat() if session.last_seen_at else None,
                "last_meter_wh": session.last_meter_wh,
                "charging_profiles": profiles,
            }
        )
    return {"sessions": items}


async def _send_error_and_close(websocket: websockets.WebSocketServerProtocol, text: str) -> None:
    await websocket.send(text)
    await websocket.close(code=1002, reason=text)


async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
    request = getattr(websocket, "request", None)
    path = getattr(websocket, "path", None)
    if path is None and request is not None:
        path = getattr(request, "path", None)
    charge_point_id = ((path or "/").lstrip("/")) or "unknown"

    session = _get_session(charge_point_id)
    session.connected = True
    session.connected_at = _now()
    session.last_seen_at = _now()
    logger.info("Client connected: %s", charge_point_id)

    try:
        request_headers = getattr(websocket, "request_headers", None)
        if request_headers is None:
            request_headers = getattr(request, "headers", None)
        if request_headers is None:
            request_headers = {}

        parent_context = propagate.extract(request_headers)
        async for message in websocket:
            session.last_seen_at = _now()
            _raw_log("RX", charge_point_id, message)
            if message == "DUMP_STATE":
                summary = _dump_state_summary()
                text = json.dumps(summary)
                _compact_log(charge_point_id, "TX", "DUMP_STATE", "-", {"sessions": len(summary["sessions"])})
                _raw_log("TX", charge_point_id, text)
                await websocket.send(text)
                continue

            with tracer.start_as_current_span("ws.message", context=parent_context) as span:
                span.set_attribute("ocpp.charge_point_id", charge_point_id)
                span.set_attribute("ws.message_length", len(message))

                try:
                    data = parse_message(message)
                except json.JSONDecodeError as exc:
                    logger.error("Invalid JSON from %s: %s", charge_point_id, exc.msg)
                    await _send_error_and_close(websocket, "ERROR: invalid JSON")
                    return

                try:
                    uid, action, payload = validate_call(data)
                except ValueError as exc:
                    uid = data[1] if isinstance(data, list) and len(data) > 1 and isinstance(data[1], str) else "UNKNOWN"
                    _compact_log(charge_point_id, "RX", "INVALID", uid, {"error": str(exc)})
                    if uid != "UNKNOWN":
                        await _send_call_error(
                            websocket,
                            charge_point_id,
                            uid,
                            "FormationViolation",
                            str(exc),
                            "INVALID",
                        )
                        continue
                    await _send_error_and_close(websocket, "ERROR: invalid CALL frame")
                    return

                summary = _summary_for(action, payload)
                _compact_log(charge_point_id, "RX", action, uid, summary)

                try:
                    _validate_payload(action, payload)
                except ValidationError as exc:
                    await _send_call_error(websocket, charge_point_id, uid, exc.code, str(exc), action)
                    continue

                if action == "BootNotification":
                    session.boot_accepted = True
                    session.boot_info = payload
                    result_payload = {
                        "status": "Accepted",
                        "currentTime": utc_now_iso_z(),
                        "interval": HEARTBEAT_INTERVAL_SECONDS,
                    }
                elif action == "Heartbeat":
                    session.last_heartbeat_at = _now()
                    result_payload = {"currentTime": utc_now_iso_z()}
                elif action == "StatusNotification":
                    session.status = str(payload.get("status"))
                    session.connector_id = int(payload.get("connectorId"))
                    result_payload = {}
                elif action == "StartTransaction":
                    global _next_transaction_id
                    transaction_id = _next_transaction_id
                    _next_transaction_id += 1
                    session.active_transaction_id = transaction_id
                    session.transactions[transaction_id] = {
                        "started_at": _now().isoformat(),
                        "meterStart": int(payload.get("meterStart")),
                        "idTag": payload.get("idTag"),
                        "connectorId": payload.get("connectorId"),
                    }
                    result_payload = {
                        "transactionId": transaction_id,
                        "idTagInfo": {"status": "Accepted"},
                    }
                    logger.info(
                        "StartTransaction: chargePointId=%s transactionId=%s",
                        charge_point_id,
                        transaction_id,
                    )
                elif action == "StopTransaction":
                    transaction_id = coerce_int(payload.get("transactionId"), "transactionId")
                    meter_stop = coerce_int(payload.get("meterStop"), "meterStop")
                    session.active_transaction_id = None
                    session.last_meter_wh = meter_stop
                    tx = session.transactions.get(transaction_id, {})
                    tx.update({"stopped_at": _now().isoformat(), "meterStop": meter_stop})
                    session.transactions[transaction_id] = tx
                    result_payload = {"idTagInfo": {"status": "Accepted"}}
                    logger.info(
                        "StopTransaction: transactionId=%s meterStop=%s",
                        transaction_id,
                        meter_stop,
                    )
                elif action == "MeterValues":
                    meter_value = payload.get("meterValue")
                    first = meter_value[0] if isinstance(meter_value, list) and meter_value else {}
                    sampled = first.get("sampledValue") if isinstance(first, dict) else []
                    first_sample = sampled[0] if isinstance(sampled, list) and sampled else {}
                    value = coerce_int(first_sample.get("value"), "sampledValue.value")
                    session.last_meter_wh = value
                    result_payload = {}
                    logger.info(
                        "MeterValues: chargePointId=%s connectorId=%s transactionId=%s timestamp=%s value=%s",
                        charge_point_id,
                        payload.get("connectorId"),
                        payload.get("transactionId"),
                        first.get("timestamp"),
                        value,
                    )
                elif action == "SetChargingProfile":
                    charging_profile = payload.get("chargingProfile", {})
                    profile_id = coerce_int(charging_profile.get("chargingProfileId"), "chargingProfileId")
                    stack_level = coerce_int(charging_profile.get("stackLevel"), "stackLevel")
                    purpose = charging_profile.get("chargingProfilePurpose")
                    schedule = charging_profile.get("chargingSchedule", {})
                    periods = schedule.get("chargingSchedulePeriod", [])
                    first = periods[0] if isinstance(periods, list) and periods else {}
                    limit = first.get("limit") if isinstance(first, dict) else None
                    session.charging_profiles[profile_id] = {
                        "profile": charging_profile,
                        "received_at": _now().isoformat(),
                        "limit_w": limit,
                        "purpose": purpose,
                        "stackLevel": stack_level,
                    }
                    result_payload = {"status": "Accepted"}
                    logger.info(
                        "SetChargingProfile: chargePointId=%s profileId=%s stackLevel=%s limit=%s purpose=%s",
                        charge_point_id,
                        profile_id,
                        stack_level,
                        limit,
                        purpose,
                    )
                elif action == "ClearChargingProfile":
                    profile_id = payload.get("chargingProfileId")
                    cleared = []
                    if profile_id is None:
                        cleared = list(session.charging_profiles.keys())
                        session.charging_profiles.clear()
                    else:
                        profile_id_int = coerce_int(profile_id, "chargingProfileId")
                        if profile_id_int in session.charging_profiles:
                            session.charging_profiles.pop(profile_id_int, None)
                            cleared = [profile_id_int]
                    result_payload = {"status": "Accepted"}
                    logger.info(
                        "ClearChargingProfile: chargePointId=%s cleared=%s",
                        charge_point_id,
                        cleared,
                    )
                else:
                    await _send_call_error(
                        websocket,
                        charge_point_id,
                        uid,
                        "NotSupported",
                        "Action not supported",
                        action,
                    )
                    continue

                await _send_call_result(websocket, charge_point_id, uid, action, result_payload)
    except ConnectionClosed:
        pass
    finally:
        session.connected = False
        session.disconnected_at = _now()
        logger.info("Client disconnected: %s", charge_point_id)


async def main() -> None:
    setup_tracing()
    setup_raw_logging()
    logger.addHandler(SpanEventHandler())
    host = os.getenv("APP_HOST", "localhost")
    port = int(os.getenv("APP_PORT", "9000"))
    logger.info("Starting server on ws://%s:%s/{chargePointId}", host, port)
    async with websockets.serve(handle_client, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
