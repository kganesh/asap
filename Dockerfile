FROM python:3.12-slim

ARG OPA_VERSION=v1.4.2
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && arch="${TARGETARCH:-amd64}" \
 && curl -fsSL -o /usr/local/bin/opa "https://github.com/open-policy-agent/opa/releases/download/${OPA_VERSION}/opa_linux_${arch}_static" \
 && chmod +x /usr/local/bin/opa && opa version \
 && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY asap ./asap
COPY policies ./policies
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"

ENV ASAP_RUNS_DIR=/app/runs PYTHONUNBUFFERED=1 COLUMNS=140
ENTRYPOINT ["asap"]
CMD ["demo", "--approve", "auto", "--no-pause"]
