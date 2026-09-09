FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY opssentinel ./opssentinel
RUN pip install . \
    && groupadd --gid 10001 opssentinel \
    && useradd --uid 10001 --gid opssentinel --no-create-home opssentinel \
    && mkdir -p /data \
    && chown opssentinel:opssentinel /data

USER opssentinel:opssentinel
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)" || exit 1
CMD ["python", "-m", "opssentinel", "--host", "0.0.0.0", "--port", "8765", "--data-dir", "/data", "--no-demo"]
