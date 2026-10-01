FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# pyproject.toml is the single source of dependencies. Installing the package
# pulls them in; there is no separate requirements file to keep in sync.
COPY pyproject.toml README.md /app/
COPY spine /app/spine
RUN pip install --no-cache-dir .

COPY tools /app/tools
# The demo (tools/quickstart.py) seeds the finance policy pack from here.
COPY examples /app/examples
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations

EXPOSE 8000

CMD ["uvicorn", "spine.main:app", "--host", "0.0.0.0", "--port", "8000"]
