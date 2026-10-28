FROM python:3.12-slim

# System packages needed by psycopg2, playwright, lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir google-genai cryptography

# Install Playwright browsers (needed by DeepSeek playwright scraper)
RUN python -m playwright install chromium --with-deps 2>/dev/null || true

# Copy source
COPY . .

# Run DB migrations on startup via CMD wrapper
# (dashboard_api.py runs migrations in its lifespan handler)
EXPOSE 8766

CMD ["python", "-m", "uvicorn", "web_ui.dashboard_api:app", "--host", "0.0.0.0", "--port", "8766"]
