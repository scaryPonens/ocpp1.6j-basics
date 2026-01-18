from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os

import websockets
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from websockets.exceptions import ConnectionClosed

from .common import make_call_result, parse_message, utc_now_iso_z

CALL = 2
CALL_RESULT = 3
CALL_ERROR = 4


logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def setup_tracing() -> None:
    service_name = os.getenv("OTEL_SERVICE_NAME", "ocpp16-server")
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


def _call_error(uid: str, code: str, description: str) -> list[object]:
    return [CALL_ERROR, uid, code, description, {}]


async def _send_error_and_close(websocket: websockets.WebSocketServerProtocol, text: str) -> None:
    await websocket.send(text)
    await websocket.close(code=1002, reason=text)


async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
    request = getattr(websocket, "request", None)
    path = getattr(websocket, "path", None)
    if path is None and request is not None:
        path = getattr(request, "path", None)
    charge_point_id = ((path or "/").lstrip("/")) or "unknown"
    logger.info("Client connected: %s", charge_point_id)

    try:
        request_headers = getattr(websocket, "request_headers", None)
        if request_headers is None:
            request_headers = getattr(request, "headers", None)
        if request_headers is None:
            request_headers = {}

        parent_context = propagate.extract(request_headers)
        async for message in websocket:
            with tracer.start_as_current_span("ws.message", context=parent_context) as span:
                span.set_attribute("ocpp.charge_point_id", charge_point_id)
                span.set_attribute("ws.message_length", len(message))
                logger.info("Received raw: %s", message)

                try:
                    data = parse_message(message)
                except json.JSONDecodeError as exc:
                    await _send_error_and_close(websocket, f"ERROR: invalid JSON ({exc.msg})")
                    return

                if not isinstance(data, list):
                    await _send_error_and_close(websocket, "ERROR: frame must be a JSON list")
                    return
                if len(data) != 4:
                    await _send_error_and_close(websocket, "ERROR: CALL frame must have length 4")
                    return

                message_type, uid, action, payload = data
                if message_type != CALL:
                    await _send_error_and_close(websocket, "ERROR: MessageTypeId must be 2 (CALL)")
                    return

                if action != "BootNotification":
                    error = _call_error(uid, "NotSupported", "Only BootNotification is supported")
                    error_text = json.dumps(error)
                    await websocket.send(error_text)
                    logger.info("Sent: %s", error_text)
                    continue

                if not isinstance(payload, dict):
                    await _send_error_and_close(websocket, "ERROR: payload must be an object")
                    return

                result_payload = {
                    "status": "Accepted",
                    "currentTime": utc_now_iso_z(),
                    "interval": 30,
                }
                response = make_call_result(uid, result_payload)
                response_text = json.dumps(response)
                await websocket.send(response_text)
                logger.info("Sent: %s", response_text)
    except ConnectionClosed:
        pass
    finally:
        logger.info("Client disconnected: %s", charge_point_id)


async def main() -> None:
    setup_tracing()
    logger.addHandler(SpanEventHandler())
    host = os.getenv("APP_HOST", "localhost")
    port = int(os.getenv("APP_PORT", "9000"))
    logger.info("Starting server on ws://%s:%s/{chargePointId}", host, port)
    async with websockets.serve(handle_client, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
