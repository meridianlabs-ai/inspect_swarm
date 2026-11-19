# The image of the bridged users-isolation tests: the ACL tools users isolation needs,
# curl, which inspect_swe's MCP readiness probe and the tests' requests use, and the
# tests' own checks.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends acl curl \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=644 checks.py /opt/checks.py
