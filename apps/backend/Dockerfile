FROM python:3.12-slim

WORKDIR /app

# Prompt-injection guard (Guardrails DetectJailbreak): CPU-only torch (no CUDA
# libraries) and its models at pinned revisions, in layers before the source
# so code changes reuse them. At runtime the models load offline.
ENV HF_HOME=/opt/models/hf \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TRANSFORMERS_VERBOSITY=error
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.4,<3" \
    && pip install --no-cache-dir "huggingface_hub>=1,<2"
COPY scripts/fetch_guardrails_models.py /tmp/fetch_guardrails_models.py
RUN python /tmp/fetch_guardrails_models.py && rm /tmp/fetch_guardrails_models.py \
    && chmod -R a+rX "$HF_HOME"
ENV HF_HUB_OFFLINE=1

COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir . \
    && useradd --create-home app \
    && install -d -m 700 -o app -g app /home/app/.local/share/prawdowid

USER app
CMD ["uvicorn", "prawdowid.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
