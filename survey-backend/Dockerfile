# Lightweight Python image — no QGIS needed for the survey API
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system deps (psycopg2 needs libpq)
RUN apt-get update -qq && \
    apt-get install -y -qq libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy project files
COPY survey-backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY survey-backend /app

# Create uploads directory
RUN mkdir -p /app/uploads

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
