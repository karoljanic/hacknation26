FROM python:3.12-slim

WORKDIR /app

# Dependencies first, from pyproject.toml alone, so editing the code does not
# reinstall them. A placeholder package lets `pip install .` resolve; the pip
# cache mount keeps downloaded wheels between builds without bloating the image.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p src/prawdowid \
    && touch src/prawdowid/__init__.py \
    && pip install . \
    && rm -rf src build

# The application itself: only this part reruns when the code changes.
COPY alembic.ini ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir --no-deps --force-reinstall . \
    && rm -rf build \
    && useradd --create-home app

USER app
CMD ["uvicorn", "prawdowid.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
