FROM python:3.12-slim

# Ensure system is up to date and basic deps are present
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Run once, then exit (Railway cron/schedule will restart it as needed)
CMD ["python", "main.py"]
