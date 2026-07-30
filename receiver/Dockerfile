# The webhook receiver: FastAPI/uvicorn/kubernetes-client. Deliberately a
# separate image from the root Dockerfile's pipeline — different runtime,
# different failure-isolation story ("a pipeline crash can't take down the
# thing that accepts alerts", docs/05). No git needed here (unlike the
# pipeline image): the receiver never runs GitCollector.
#
# Build from the repo root so both `agent/state.py` (reused for the Incident/
# Window dataclasses — zero external deps, confirmed) and `receiver/` are in
# the build context:
#   docker build -f receiver/Dockerfile -t postmortem-receiver:dev .

FROM python:3.12-alpine AS builder

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY receiver/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

FROM python:3.12-alpine

RUN addgroup -g 10001 app && \
    adduser -D -H -u 10001 -G app -s /sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Only agent/state.py's own dependency-free contract, not the rest of
# agent/ — the receiver has no business importing anything that needs
# langgraph/neo4j/anthropic, and copying only what's used makes that a fact
# about the image, not just an intention in a docstring.
COPY --chown=app:app agent/__init__.py agent/__init__.py
COPY --chown=app:app agent/state.py agent/state.py
COPY --chown=app:app receiver/ receiver/

USER 10001:10001

EXPOSE 8000
ENTRYPOINT ["uvicorn", "receiver.main:app", "--host", "0.0.0.0", "--port", "8000"]
