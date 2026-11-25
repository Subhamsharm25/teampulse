# Multi-stage build; the runtime image runs as a non-root user and keeps the SQLite file in /data.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
LABEL org.opencontainers.image.title="teampulse" \
      org.opencontainers.image.description="Daily hours, attendance and utilization dashboard for small teams" \
      org.opencontainers.image.source="https://github.com/Subhamsharm25/teampulse" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin teampulse \
 && mkdir /data && chown teampulse /data
COPY --from=build /install /usr/local
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TEAMPULSE_DB=/data/teampulse.db
USER 10001
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
ENTRYPOINT ["python", "-m", "teampulse"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
