# Multi-stage build for the pipeline image. Target ~180 MB (docs/04-tech-stack.md).
#
# This image is the pipeline only — LangGraph, Neo4j driver, the LLM client,
# `agent/`. It does NOT contain FastAPI/uvicorn/kubernetes-client; the webhook
# receiver is a separate, always-on service with a different failure-isolation
# story ("a pipeline crash can't take down the thing that accepts alerts",
# docs/05) and gets its own Dockerfile under receiver/.
#
# Alpine, not slim-Debian, and the reason is measured, not assumed: Debian's
# `git` package pulls perl + libcurl + gnutls + krb5 as hard Depends (not
# recommends — `--no-install-recommends` doesn't touch them), which alone cost
# 99 MB, more than half the image, on a first build against python:3.12-slim.
# Alpine's git has no such chain. The risk this trades in is musl vs. glibc:
# a dependency without musllinux wheels would compile from source in the
# builder stage instead of installing a prebuilt wheel — verified this
# doesn't happen for anything in agent/requirements.txt (pip install logs show
# wheels, not `Building wheel for ...`), and the resulting image was smoke-
# tested with a real pipeline run, not just `--help`.

# --- builder ---------------------------------------------------------------
FROM python:3.12-alpine AS builder

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY agent/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# --- runtime -----------------------------------------------------------
FROM python:3.12-alpine

# git is needed at runtime: GitCollector shells out to it
# (agent/collectors/git.py). Alpine's package has no perl/curl/gnutls chain.
RUN apk add --no-cache git

# A fixed, known uid/gid rather than a nameless allocation — k3s security
# contexts (runAsUser/runAsNonRoot) match against the number, and a fixed
# value keeps the Helm chart's securityContext block correct without having
# to inspect the image to find out what got assigned.
RUN addgroup -g 10001 app && \
    adduser -D -H -u 10001 -G app -s /sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Baked in as the fallback: PROMPTS_DIR unset (the default) means "read
# these". A k3s ConfigMap mount overrides the directory without a rebuild —
# the whole reason prompts are .md files (agent/prompts/__init__.py).
COPY --chown=app:app agent/ agent/
COPY --chown=app:app config/ config/

# The two golden incidents that already cleared the Phase 1 gate — 900 KB
# total — so `kubectl create job --from=cronjob/postmortem-manual` (docs/05's
# "1. Manual" trigger path) has something real to run without a live dataset
# mounted in. Stated here rather than left as an accident of a broad COPY.
COPY --chown=app:app evals/incidents/inc-0001-missing-env-var/ \
     evals/incidents/inc-0001-missing-env-var/
COPY --chown=app:app evals/incidents/inc-0004-two-deploys/ \
     evals/incidents/inc-0004-two-deploys/

USER 10001:10001

ENTRYPOINT ["python", "-m", "agent.cli"]
CMD ["--help"]
