#!/usr/bin/env bash
# GCP VM setup — Docker on port 80 (same pattern as AI UseCase).
# Run from the repo root AFTER: git clone ... && cd ai-stock-trading-bot
# Usage: bash scripts/vm_setup.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Installing Docker (if needed)"
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$(whoami)" || true
fi

if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo ""
  echo "WARNING: .env is missing."
  if [[ -f "$REPO_ROOT/.env.example" ]]; then
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    chmod 600 "$REPO_ROOT/.env"
    echo "Created .env from .env.example — edit keys before relying on live APIs:"
    echo "  nano $REPO_ROOT/.env"
  fi
else
  chmod 600 "$REPO_ROOT/.env"
  echo "==> Found existing .env"
fi

echo "==> Stopping old systemd service if present"
if systemctl list-unit-files | grep -q '^trading-bot.service'; then
  sudo systemctl disable --now trading-bot 2>/dev/null || true
fi

echo "==> Building and starting container (host :80 → container :8080)"
if docker info >/dev/null 2>&1; then
  docker compose up -d --build
else
  sudo docker compose up -d --build
fi

echo ""
echo "Done. Same access pattern as AI UseCase:"
echo "  Open:  http://YOUR_EXTERNAL_IP"
echo "  Logs:  docker compose logs -f bot"
echo "  Stop:  docker compose down"
echo "  Update: git pull && docker compose up -d --build"
echo ""
echo "GCP: keep 'Allow HTTP traffic' ON (port 80)."
echo "If docker permission denied, log out/in SSH once, or use sudo docker ..."
