FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY ocpp16_min ./ocpp16_min
RUN pip install --no-cache-dir .

ENV APP_HOST=0.0.0.0
ENV APP_PORT=9000

CMD ["python", "-m", "ocpp16_min.server"]
