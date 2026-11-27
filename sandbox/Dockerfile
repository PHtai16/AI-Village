FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash sandboxuser \
    && pip install --no-cache-dir pytest>=8.0

USER sandboxuser
WORKDIR /workspace
