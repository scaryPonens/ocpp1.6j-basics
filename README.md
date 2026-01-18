# laughing-octo-guacamole

Minimal OCPP 1.6-J BootNotification exchange (happy path only) between a
charge point emulator and a server over WebSockets.

## Prerequisites

- Python 3.10+
- `uv` (https://docs.astral.sh/uv/)

## Setup (uv)

```bash
uv venv
uv sync
```

## Run

### Terminal 1: server

```bash
uv run python -m ocpp16_min.server
```

Server listens on `ws://localhost:9000/{chargePointId}`.

### Terminal 2: client

```bash
uv run python -m ocpp16_min.client
```

Client connects to `ws://localhost:9000/CP_1`, sends a BootNotification,
sends one StatusNotification (Available), then StartTransaction and
StopTransaction (after ~10 seconds). Heartbeats run during the
simulated session and MeterValues are sent every 5 seconds. Exits with
code 0 only on success.

## Smart Charging (minimal)

This repo implements only a tiny subset of Smart Charging:
- Supports `SetChargingProfile` and `ClearChargingProfile` with a minimal profile.
- Profiles are stored and visible in the state dump.
- Limits are **not** enforced; this is protocol exploration only.

Example `SetChargingProfile` payload:
```json
{
  "connectorId": 1,
  "chargingProfile": {
    "chargingProfileId": 1,
    "stackLevel": 1,
    "chargingProfilePurpose": "TxProfile",
    "chargingProfileKind": "Absolute",
    "chargingSchedule": {
      "chargingRateUnit": "W",
      "chargingSchedulePeriod": [
        { "startPeriod": 0, "limit": 7000 }
      ]
    }
  }
}
```

## Logging and State Dump

- Raw frames are written to `logs/ocpp_raw.log` (rotating).
- To dump server state, send the text message `DUMP_STATE` over any active
  WebSocket connection. The server replies with a compact JSON summary,
  including active charging profiles (ids and limits).

## Tracing (OpenTelemetry + Jaeger)

This project emits traces via OpenTelemetry OTLP. Client and server propagate
trace context over WebSockets, so spans appear in one distributed trace.

Environment variables:
- `OTEL_SERVICE_NAME` (default: `ocpp16-server` for server, `ocpp16-client` for client)
- `OTEL_EXPORTER_OTLP_ENDPOINT` (default: `http://localhost:4317`)

To run Jaeger locally:
```bash
docker compose up --build
```

## Expected Output (brief)

**Server**
```
INFO - Client connected: CP_1
2026-01-18T12:34:56Z cp=CP_1 dir=RX action=BootNotification uid=... summary={'vendor': 'RalphCo', 'model': 'RalphModel1'}
2026-01-18T12:34:56Z cp=CP_1 dir=TX action=BootNotification uid=... summary={'result': True}
2026-01-18T12:34:57Z cp=CP_1 dir=RX action=StatusNotification uid=... summary={'connectorId': 0, 'status': 'Available', 'errorCode': 'NoError'}
INFO - StatusNotification: connectorId=0 status=Available
2026-01-18T12:34:58Z cp=CP_1 dir=RX action=StartTransaction uid=... summary={'connectorId': 1, 'idTag': 'TEST', 'meterStart': 0}
INFO - StartTransaction: chargePointId=CP_1 transactionId=1
2026-01-18T12:34:59Z cp=CP_1 dir=RX action=SetChargingProfile uid=... summary={'profileId': 1, 'stackLevel': 1, 'purpose': 'TxProfile', 'limit': 7000}
INFO - SetChargingProfile: chargePointId=CP_1 profileId=1 stackLevel=1 limit=7000 purpose=TxProfile
2026-01-18T12:35:02Z cp=CP_1 dir=RX action=MeterValues uid=... summary={'connectorId': 1, 'transactionId': 1}
INFO - MeterValues: chargePointId=CP_1 connectorId=1 transactionId=1 timestamp=... value=100
2026-01-18T12:35:07Z cp=CP_1 dir=RX action=MeterValues uid=... summary={'connectorId': 1, 'transactionId': 1}
INFO - MeterValues: chargePointId=CP_1 connectorId=1 transactionId=1 timestamp=... value=200
2026-01-18T12:35:08Z cp=CP_1 dir=RX action=ClearChargingProfile uid=... summary={'profileId': 1}
INFO - ClearChargingProfile: chargePointId=CP_1 cleared=[1]
2026-01-18T12:35:08Z cp=CP_1 dir=RX action=StopTransaction uid=... summary={'transactionId': 1, 'meterStop': 200}
INFO - StopTransaction: transactionId=1 meterStop=200
INFO - Client disconnected: CP_1
```

**Client**
```
BootNotification RAW RESPONSE: [3,"... ",{"status":"Accepted","currentTime":"2026-01-18T12:34:56Z","interval":10}]
BootNotification RESPONSE: CALLRESULT uid=...
StatusNotification sent (Available)
StatusNotification RAW RESPONSE: [3,"... ",{}]
StatusNotification RESPONSE: CALLRESULT uid=...
StatusNotification acknowledged
StartTransaction sent (connectorId=1)
StartTransaction RAW RESPONSE: [3,"... ",{"transactionId":1,"idTagInfo":{"status":"Accepted"}}]
StartTransaction RESPONSE: CALLRESULT uid=...
StartTransaction acknowledged (transactionId=1)
SetChargingProfile sent (profileId=1 limit_kw=7.0)
SetChargingProfile RAW RESPONSE: [3,"... ",{"status":"Accepted"}]
SetChargingProfile RESPONSE: CALLRESULT uid=...
SetChargingProfile acknowledged (profileId=1)
Heartbeat 1 sent
Heartbeat 1 RAW RESPONSE: [3,"... ",{"currentTime":"2026-01-18T12:35:06Z"}]
Heartbeat 1 RESPONSE: CALLRESULT uid=...
Heartbeat 1 acknowledged
MeterValues sent (energy_wh=100)
MeterValues RAW RESPONSE: [3,"... ",{}]
MeterValues RESPONSE: CALLRESULT uid=...
MeterValues acknowledged
MeterValues sent (energy_wh=200)
MeterValues RAW RESPONSE: [3,"... ",{}]
MeterValues RESPONSE: CALLRESULT uid=...
MeterValues acknowledged
ClearChargingProfile sent (profileId=1)
ClearChargingProfile RAW RESPONSE: [3,"... ",{"status":"Accepted"}]
ClearChargingProfile RESPONSE: CALLRESULT uid=...
ClearChargingProfile acknowledged (profileId=1)
StopTransaction sent (transactionId=1)
StopTransaction RAW RESPONSE: [3,"... ",{"idTagInfo":{"status":"Accepted"}}]
StopTransaction RESPONSE: CALLRESULT uid=...
StopTransaction acknowledged (transactionId=1)
```