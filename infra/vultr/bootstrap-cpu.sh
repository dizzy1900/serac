#!/bin/bash
# Bootstrap for a Vultr x86 CPU box: Docker + uv + both repos, images built from HEAD.
# Everything logs to /var/log/bootstrap.log; /root/BOOTSTRAP_DONE marks success.
set -euxo pipefail
exec > >(tee -a /var/log/bootstrap.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git make build-essential tmux htop jq
curl -fsSL https://get.docker.com | sh
curl -LsSf https://astral.sh/uv/0.11.7/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
# Self-destruct guard: power off after MAX_HOURS so a forgotten box cannot drain credit.
MAX_HOURS="${MAX_HOURS:-72}"
echo "shutdown -h +$((MAX_HOURS*60))" | bash || true
mkdir -p /work && cd /work
git clone https://github.com/dizzy1900/serac.git
git clone https://github.com/dizzy1900/rupture.git
rc=0; ( cd serac && docker build -f infra/docker/Dockerfile -t serac:$(git rev-parse --short HEAD) . ) > /work/serac-build.log 2>&1 || rc=$?; echo "serac build rc=$rc" >> /work/build-status
rc=0; ( cd rupture && docker build -f infra/docker/Dockerfile --build-arg GIT_SHA=$(git rev-parse HEAD) --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) -t rupture:$(git rev-parse --short HEAD) . ) > /work/rupture-build.log 2>&1 || rc=$?; echo "rupture build rc=$rc" >> /work/build-status
docker pull openquake/engine:3.26.2 >> /work/build-status 2>&1 || echo "oq pull failed" >> /work/build-status
touch /root/BOOTSTRAP_DONE
