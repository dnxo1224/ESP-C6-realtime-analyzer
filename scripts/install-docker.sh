#!/usr/bin/env bash
# NCP Ubuntu 24.04에 Docker CE를 공식 저장소(서명 검증)로 설치한다.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

timedatectl set-timezone Asia/Seoul || true

apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

echo "=== 설치 결과 ==="
docker --version
docker compose version
date
df -h / | tail -1
