# ============================================================
# AI Stock Trading Bot — Dockerfile
# No apt-get: all deps ship as prebuilt wheels (works on
# locked-down VMs that cannot reach deb.debian.org).
# ============================================================

FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim AS runtime

RUN groupadd -r botuser && useradd -r -g botuser -d /app -s /sbin/nologin botuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY . .

RUN mkdir -p /app/data /app/logs && \
    chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    TRADE_JOURNAL_PATH=/app/data/trade_journal.db \
    INDIA_PAPER_PORTFOLIO_PATH=/app/data/india_paper_portfolio.json

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=5)" || exit 1

STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
