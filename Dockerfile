# =============================================================================
# SAT Zone — Telegram bot service image.
# Runs long polling + the internal OTP-push HTTP server (port 8081).
# =============================================================================
FROM python:3.13-slim

# Predictable, log-friendly Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (declared in pyproject) for better layer caching,
# then the project itself.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# Run as a non-root user — the bot needs no elevated privileges.
RUN useradd --create-home --uid 1000 botuser
USER botuser

# Internal OTP-push endpoint the API calls.
EXPOSE 8081

# Liveness probe — relies on the unauthenticated /healthz endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3).status==200 else 1)"

CMD ["python", "-m", "app.main"]
