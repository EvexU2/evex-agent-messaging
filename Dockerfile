FROM python:3.13.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 evex && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent evex
COPY src/evex_agent_messaging /app/evex_agent_messaging
USER 10001:10001
ENTRYPOINT ["python", "-m", "evex_agent_messaging"]
