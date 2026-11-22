FROM python:3.12-slim

WORKDIR /app

# Prompt-injection guard (Guardrails DetectJailbreak): CPU-only torch (no CUDA
# libraries) and its models at pinned revisions, in layers before the source
# so code changes reuse them. At runtime the models load offline.
ENV HF_HOME=/opt/models/hf \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TRANSFORMERS_VERBOSITY=error
# Optional build secret `build_ca`: an extra CA bundle for builds behind a TLS-inspecting proxy.
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/build_ca > /tmp/build-ca.pem; \
        export PIP_CERT=/tmp/build-ca.pem SSL_CERT_FILE=/tmp/build-ca.pem REQUESTS_CA_BUNDLE=/tmp/build-ca.pem; \
    fi; \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.4,<3" \
    && pip install --no-cache-dir "huggingface_hub>=1,<2" \
    && rm -f /tmp/build-ca.pem
COPY scripts/fetch_guardrails_models.py /tmp/fetch_guardrails_models.py
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/build_ca > /tmp/build-ca.pem; \
        export PIP_CERT=/tmp/build-ca.pem SSL_CERT_FILE=/tmp/build-ca.pem REQUESTS_CA_BUNDLE=/tmp/build-ca.pem; \
    fi; \
    mkdir -p "$HF_HOME" && python /tmp/fetch_guardrails_models.py && rm /tmp/fetch_guardrails_models.py /tmp/build-ca.pem -f \
    && chmod -R a+rX "$HF_HOME"
ENV HF_HUB_OFFLINE=1

COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY examples ./examples
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/build_ca > /tmp/build-ca.pem; \
        export PIP_CERT=/tmp/build-ca.pem SSL_CERT_FILE=/tmp/build-ca.pem REQUESTS_CA_BUNDLE=/tmp/build-ca.pem; \
    fi; \
    pip install --no-cache-dir . \
    && rm -f /tmp/build-ca.pem \
    && useradd --create-home app \
    && install -d -m 700 -o app -g app /home/app/.local/share/prawdowid

USER app
CMD ["uvicorn", "prawdowid.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
