from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import uuid

import websockets
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .common import make_call, parse_message

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


async def main() -> int:
    setup_tracing()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    logger.addHandler(SpanEventHandler())
    uri = "ws://localhost:9000/CP_1"
    uid = str(uuid.uuid4())
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
        except ConnectionRefusedError:
            logger.error("Could not connect to server")
            return 1

        logger.info("RAW RESPONSE: %s", response_text)
        try:
            response = parse_message(response_text)
        except json.JSONDecodeError:
            logger.error("PARSED RESPONSE: <invalid JSON>")
            return 1

        logger.info("PARSED RESPONSE: %s", response)
        if (
            isinstance(response, list)
            and len(response) >= 3
            and response[0] == 3
            and response[1] == uid
            and isinstance(response[2], dict)
            and response[2].get("status") == "Accepted"
        ):
            span.set_attribute("ws.response_status", "Accepted")
            return 0
        span.set_attribute("ws.response_status", "Rejected")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
