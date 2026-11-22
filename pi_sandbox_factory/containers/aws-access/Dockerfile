FROM docker.io/library/debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3-botocore ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY signer.py receive.py .
RUN python3 signer.py --build-catalogue
ENTRYPOINT ["python3", "/app/signer.py"]
