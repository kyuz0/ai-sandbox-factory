FROM docker.io/library/python:3.13-slim-trixie@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=46011051142-1
# The broker uses only the standard library and never installs Python packages.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip uninstall --yes pip \
    && python3 -c 'import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)'
WORKDIR /app
COPY broker.py receive.py ./
ENTRYPOINT ["python3", "-B", "/app/broker.py"]
