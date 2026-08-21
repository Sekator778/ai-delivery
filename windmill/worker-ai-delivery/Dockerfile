# Custom Windmill worker image — the "ai-delivery" worker group.
#
# Base: the official Windmill worker (Debian 12 bookworm).
#   Already includes: node 20, git, curl, python3, bash + Claude Code 2.1.145 (/usr/bin/claude).
# We add only: JDK 21 (Eclipse Temurin, the Adoptium apt repository) + Maven.
#
# Purpose — the heavy pipeline stages on this worker:
#   spec / plan / test-design / implement / review → running `claude` (the harness);
#   gate → `mvn verify` (tests + JaCoCo + SpotBugs + Checkstyle).
#
# Build:  docker build -t ai-delivery-worker:local windmill/worker-ai-delivery
FROM ghcr.io/windmill-labs/windmill:main

USER root

# JDK 21 — Eclipse Temurin from the official Adoptium apt repository (per the adoptium.net docs),
# Maven — from the Debian repository.
RUN apt-get update \
 && apt-get install -y --no-install-recommends wget apt-transport-https gpg ca-certificates \
 && wget -qO - https://packages.adoptium.net/artifactory/api/gpg/key/public \
      | gpg --dearmor | tee /etc/apt/trusted.gpg.d/adoptium.gpg > /dev/null \
 && echo "deb https://packages.adoptium.net/artifactory/deb $(awk -F= '/^VERSION_CODENAME/{print$2}' /etc/os-release) main" \
      | tee /etc/apt/sources.list.d/adoptium.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends temurin-21-jdk maven \
 && ln -sfn "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" /opt/java-home \
 && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/opt/java-home

# A build-time check — fail early if a tool is missing.
# claude is already in the Windmill base image — no need to install it separately.
RUN java -version && mvn -version && claude --version && git --version
