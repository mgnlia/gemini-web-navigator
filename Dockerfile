FROM python:3.11-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libxshmfence1 libx11-6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
RUN pip install uv

# Copy project files
COPY pyproject.toml .
COPY agent.py .
COPY main.py .
COPY static/ ./static/

# Install dependencies directly (not editable)
RUN uv pip install --system \
    "google-genai>=1.0.0" \
    "playwright>=1.42.0" \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "pillow>=10.0.0" \
    "python-multipart>=0.0.9" \
    "pydantic>=2.0.0" \
    "httpx>=0.27.0"

# Install Playwright browsers
RUN playwright install chromium
RUN playwright install-deps chromium

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
