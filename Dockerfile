# ============================================================
# PrimeTradeML - Dockerized Batch Signal Pipeline
# ============================================================
# Build:  docker build -t mlops-task .
# Run:    docker run --rm mlops-task
# ============================================================

FROM python:3.9-slim

# Prevent Python from writing .pyc files and enable unbuffered stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline source and data
COPY run.py .
COPY config.yaml .
COPY data.csv .

# Default command: run the full pipeline
CMD ["python", "run.py", \
     "--input",    "data.csv", \
     "--config",   "config.yaml", \
     "--output",   "metrics.json", \
     "--log-file", "run.log"]
