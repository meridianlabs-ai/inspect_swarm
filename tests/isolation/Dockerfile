# The image of the users-isolation Docker tests: ACL tools for shares, git and sudo
# (with no sudoers entry for members). The files owned by ids in the swarm's uid window
# check that setup chooses ids that own nothing.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends acl git sudo \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir /opt/owned \
    && touch /opt/owned/by-1000 /opt/owned/by-61000 \
    && chown 1000:1000 /opt/owned/by-1000 \
    && chown 61000:61001 /opt/owned/by-61000
