from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys

import websockets
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .common import (
    AVAILABLE,
    BOOTING,
    is_call_result,
    make_call,
    make_clear_charging_profile_call,
    make_heartbeat_call,
    make_set_charging_profile_call,
    make_status_notification_call,
    make_start_transaction_call,
    make_stop_transaction_call,
    make_meter_values_call,
    new_uid,
    parse_call_result_payload,
    parse_message,
    SessionState,
)

logger = logging.getLogger(__name__)


def _boot_notification_payload() -> dict[str, str]:
    return {
        "chargePointVendor": "RalphCo",
        "chargePointModel": "RalphModel1",
        "firmwareVersion": "0.1.0",
        "meterType": "RalphMeter",
    }


def setup_tracing() -> None:
    service_name = os.getenv("OTEL_SERVICE_NAME", "ocpp16-client")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)


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


def _parse_response(
    label: str,
    response_text: str,
    expected_uid: str,
) -> tuple[dict[str, object] | None, bool]:
    logger.info("%s RAW RESPONSE: %s", label, response_text)
    try:
        response = parse_message(response_text)
    except json.JSONDecodeError:
        logger.error("%s PARSE ERROR: invalid JSON", label)
        return None, True
    if not isinstance(response, list) or len(response) < 3:
        logger.error("%s PARSE ERROR: response must be a list", label)
        return None, True
    msg_type = response[0]
    uid = response[1] if len(response) > 1 else None
    if expected_uid and uid != expected_uid:
        logger.error("%s PARSE ERROR: uid mismatch (got %s)", label, uid)
        return None, True
    if msg_type == 3:
        logger.info("%s RESPONSE: CALLRESULT uid=%s", label, uid)
        try:
            payload = parse_call_result_payload(response)
        except ValueError as exc:
            logger.error("%s PARSE ERROR: %s", label, exc)
            return None, True
        return payload, False
    if msg_type == 4:
        code = response[2] if len(response) > 2 else "Unknown"
        description = response[3] if len(response) > 3 else "Unknown"
        logger.error("%s RESPONSE: CALLERROR uid=%s code=%s message=%s", label, uid, code, description)
        return None, True
    logger.error("%s PARSE ERROR: unexpected message type %s", label, msg_type)
    return None, True


async def _heartbeat_loop(
    websocket: websockets.WebSocketClientProtocol,
    interval: int,
    ws_lock: asyncio.Lock,
    error_event: asyncio.Event,
    max_count: int = 3,
) -> None:
    for idx in range(max_count):
        await asyncio.sleep(interval)
        heartbeat_call = make_heartbeat_call()
        heartbeat_uid = heartbeat_call[1]
        async with ws_lock:
            await websocket.send(json.dumps(heartbeat_call))
            logger.info("Heartbeat %s sent", idx + 1)
            heartbeat_response_text = await websocket.recv()
        _, failed = _parse_response(f"Heartbeat {idx + 1}", heartbeat_response_text, heartbeat_uid)
        if failed:
            error_event.set()
            return
        logger.info("Heartbeat %s acknowledged", idx + 1)


async def _meter_values_loop(
    websocket: websockets.WebSocketClientProtocol,
    interval: int,
    ws_lock: asyncio.Lock,
    transaction_id: int,
    energy_state: dict[str, int],
    stop_event: asyncio.Event,
    error_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(interval)
        if stop_event.is_set():
            return
        energy_state["value"] += 100
        meter_call = make_meter_values_call(
            connector_id=1,
            transaction_id=transaction_id,
            energy_wh=energy_state["value"],
        )
        meter_uid = meter_call[1]
        async with ws_lock:
            await websocket.send(json.dumps(meter_call))
            logger.info("MeterValues sent (energy_wh=%s)", energy_state["value"])
            meter_response_text = await websocket.recv()
        _, failed = _parse_response("MeterValues", meter_response_text, meter_uid)
        if failed:
            error_event.set()
            return
        logger.info("MeterValues acknowledged")


async def main() -> int:
    setup_tracing()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    logger.addHandler(SpanEventHandler())
    state = BOOTING
    host = os.getenv("APP_HOST", "localhost")
    port = int(os.getenv("APP_PORT", "9000"))
    uri = f"ws://{host}:{port}/CP_1"
    uid = new_uid()
    payload = _boot_notification_payload()
    message = json.dumps(make_call(uid, "BootNotification", payload))
    with trace.get_tracer(__name__).start_as_current_span("ws.client") as span:
        try:
            carrier: dict[str, str] = {}
            span.set_attribute("ws.uri", uri)
            span.set_attribute("ws.message", message)
            propagate.inject(carrier)
            connect_kwargs = {"additional_headers": carrier}
            try:
                connect_ctx = websockets.connect(uri, **connect_kwargs)
            except TypeError:
                connect_ctx = websockets.connect(uri, extra_headers=carrier)

            async with connect_ctx as websocket:
                span.set_attribute("ws.connected", True)
                await websocket.send(message)
                response_text = await websocket.recv()
                span.set_attribute("ws.response_text", response_text)
                boot_payload, failed = _parse_response("BootNotification", response_text, uid)
                if failed or boot_payload is None:
                    span.set_attribute("ws.response_status", "Invalid")
                    return 1
                status = boot_payload.get("status")
                if status != "Accepted":
                    logger.error("BootNotification not accepted: %s", status)
                    span.set_attribute("ws.response_status", "Rejected")
                    return 1

                interval = boot_payload.get("interval")
                if not isinstance(interval, int) or interval <= 0:
                    interval = 10
                span.set_attribute("ocpp.heartbeat_interval", interval)
                span.set_attribute("ws.response_status", "Accepted")
                logger.info("Boot accepted. Heartbeat interval=%s", interval)

                state = AVAILABLE
                logger.info("State transition: %s -> %s", BOOTING, state)
                status_call = make_status_notification_call(connector_id=0, status="Available", error_code="NoError")
                status_uid = status_call[1]
                await websocket.send(json.dumps(status_call))
                logger.info("StatusNotification sent (Available)")
                status_response_text = await websocket.recv()
                _, failed = _parse_response("StatusNotification", status_response_text, status_uid)
                if failed:
                    return 1
                logger.info("StatusNotification acknowledged")

                start_call = make_start_transaction_call(
                    connector_id=1,
                    id_tag="TEST",
                    meter_start=0,
                )
                start_uid = start_call[1]
                await websocket.send(json.dumps(start_call))
                logger.info("StartTransaction sent (connectorId=1)")
                start_response_text = await websocket.recv()
                start_payload, failed = _parse_response("StartTransaction", start_response_text, start_uid)
                if failed or start_payload is None:
                    return 1
                transaction_id = start_payload.get("transactionId")
                id_tag_info = start_payload.get("idTagInfo", {})
                if not isinstance(transaction_id, int):
                    logger.error("StartTransaction: missing transactionId")
                    return 1
                if not isinstance(id_tag_info, dict) or id_tag_info.get("status") != "Accepted":
                    logger.error("StartTransaction not accepted")
                    return 1
                session = SessionState(
                    transaction_id=transaction_id,
                    connector_id=1,
                    meter_start=0,
                    meter_stop=0,
                )
                logger.info("StartTransaction acknowledged (transactionId=%s)", session.transaction_id)

                ws_lock = asyncio.Lock()
                error_event = asyncio.Event()
                profile_id = 1
                set_profile_call = make_set_charging_profile_call(
                    connector_id=1,
                    profile_id=profile_id,
                    limit_kw=7.0,
                )
                set_profile_uid = set_profile_call[1]
                async with ws_lock:
                    await websocket.send(json.dumps(set_profile_call))
                    logger.info("SetChargingProfile sent (profileId=%s limit_kw=7.0)", profile_id)
                    set_profile_response_text = await websocket.recv()
                set_profile_payload, failed = _parse_response(
                    "SetChargingProfile",
                    set_profile_response_text,
                    set_profile_uid,
                )
                if failed or set_profile_payload is None:
                    return 1
                if set_profile_payload.get("status") != "Accepted":
                    logger.error("SetChargingProfile not accepted")
                    return 1
                logger.info("SetChargingProfile acknowledged (profileId=%s)", profile_id)

                heartbeat_task = asyncio.create_task(
                    _heartbeat_loop(websocket, interval, ws_lock, error_event, max_count=3)
                )
                meter_stop_event = asyncio.Event()
                energy_state = {"value": session.meter_start}
                meter_task = asyncio.create_task(
                    _meter_values_loop(
                        websocket,
                        interval=5,
                        ws_lock=ws_lock,
                        transaction_id=session.transaction_id,
                        energy_state=energy_state,
                        stop_event=meter_stop_event,
                        error_event=error_event,
                    )
                )
                await asyncio.sleep(10)
                meter_stop_event.set()
                await meter_task
                if error_event.is_set():
                    heartbeat_task.cancel()
                    return 1

                clear_profile_call = make_clear_charging_profile_call(profile_id=profile_id)
                clear_profile_uid = clear_profile_call[1]
                async with ws_lock:
                    await websocket.send(json.dumps(clear_profile_call))
                    logger.info("ClearChargingProfile sent (profileId=%s)", profile_id)
                    clear_profile_response_text = await websocket.recv()
                clear_profile_payload, failed = _parse_response(
                    "ClearChargingProfile",
                    clear_profile_response_text,
                    clear_profile_uid,
                )
                if failed or clear_profile_payload is None:
                    return 1
                if clear_profile_payload.get("status") != "Accepted":
                    logger.error("ClearChargingProfile not accepted")
                    return 1
                logger.info("ClearChargingProfile acknowledged (profileId=%s)", profile_id)

                session.meter_stop = energy_state["value"]
                stop_call = make_stop_transaction_call(
                    transaction_id=session.transaction_id,
                    id_tag="TEST",
                    meter_stop=session.meter_stop,
                    reason="Local",
                )
                stop_uid = stop_call[1]
                async with ws_lock:
                    await websocket.send(json.dumps(stop_call))
                    logger.info("StopTransaction sent (transactionId=%s)", session.transaction_id)
                    stop_response_text = await websocket.recv()
                stop_payload, failed = _parse_response("StopTransaction", stop_response_text, stop_uid)
                if failed or stop_payload is None:
                    return 1
                stop_tag_info = stop_payload.get("idTagInfo", {})
                if not isinstance(stop_tag_info, dict) or stop_tag_info.get("status") != "Accepted":
                    logger.error("StopTransaction not accepted")
                    return 1
                logger.info("StopTransaction acknowledged (transactionId=%s)", session.transaction_id)

                await heartbeat_task
                if error_event.is_set():
                    return 1
                return 0
        except ConnectionRefusedError:
            logger.error("Could not connect to server at %s", uri)
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
