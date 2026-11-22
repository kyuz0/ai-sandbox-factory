FROM docker.io/library/python:3.13-slim-trixie@sha256:48d884813cf44e927cf64c5529dd630dd3b18338c6dc727991eef9b71e934a20
WORKDIR /app
COPY broker.py receive.py ./
ENTRYPOINT ["python3", "-B", "/app/broker.py"]
