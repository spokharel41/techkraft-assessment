# Multi-stage Dockerfile for TechKraft Flask API
# Stage 1: Builder — installs deps and compiles any C extensions
# Stage 2: Production — minimal image, non-root user, health check

# ============================================================
# STAGE 1: builder
# ============================================================
FROM python:3.11-slim AS builder

# Install build dependencies for any packages with C extensions
# (e.g., cryptography, psutil, mysqlclient)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only requirements first — Docker layer cache optimization
# If requirements.txt doesn't change, this layer is cached
COPY requirements.txt .

# Install into a target directory so we can copy just the packages
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --target=/build/packages -r requirements.txt

# ============================================================
# STAGE 2: production
# ============================================================
FROM python:3.11-alpine

# Security: run as non-root user
# Create group and user with no home directory, no shell
RUN addgroup -S appgroup && adduser -S appuser -G appgroup -H -s /sbin/nologin

WORKDIR /app

# Copy compiled packages from builder stage
COPY --from=builder /build/packages /app/packages

# Copy application source
COPY app.py .

# Set PYTHONPATH so packages directory is found
ENV PYTHONPATH="/app/packages" \
    # Prevent Python from writing .pyc files (keeps image clean)
    PYTHONDONTWRITEBYTECODE=1 \
    # Force stdout/stderr to be unbuffered (important for log streaming)
    PYTHONUNBUFFERED=1 \
    # Flask production settings
    FLASK_ENV=production \
    FLASK_APP=app.py

# Switch to non-root user before exposing ports
USER appuser

# Expose the application port
EXPOSE 5000

# Health check — polls /health endpoint every 30s
# Start period: 10s grace time for startup
# Retries: 3 failures before marking unhealthy
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:5000/health || exit 1

# Use gunicorn for production (not Flask's built-in dev server)
# -w 2: 2 worker processes (adjust based on CPU: 2 * CPUs + 1 is typical)
# --bind: listen on all interfaces
# --access-logfile -: log to stdout for container log collection
# --error-logfile -: log errors to stderr
CMD ["python", "-m", "gunicorn", \
     "--workers", "2", \
     "--bind", "0.0.0.0:5000", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "app:app"]
