FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir . \
    && useradd --create-home app

USER app
CMD ["uvicorn", "prawdowid.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
