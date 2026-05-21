#!/usr/bin/env bash
# First-time setup on a Manjaro rip machine.
# Idempotent — safe to re-run.

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "ERROR: .env missing. Copy .env.example to .env and fill in." >&2
  exit 1
fi

echo "==> Installing system packages"
sudo pacman -S --needed --noconfirm \
  docker docker-compose \
  cifs-utils \
  lsscsi \
  nvidia-container-toolkit \
  jq go-yq ffmpeg

echo "==> Enabling docker service"
sudo systemctl enable --now docker

if ! groups "$USER" | grep -qw docker; then
  echo "==> Adding $USER to docker group (log out/in for this to take effect)"
  sudo usermod -aG docker "$USER"
fi

echo "==> Configuring NVIDIA container runtime"
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

echo "==> Creating local volume directories"
mkdir -p home media logs db

# MakeMKV track-selection rule. makemkvcon ignores --profile= (undocumented;
# see www.makemkv.com/developers/usage.txt and ARM issue #944). The actual
# CLI mechanism is app_DefaultSelectionString in ~/.MakeMKV/settings.conf,
# which lives in the bind-mounted ./home/ dir.
#
# Default rule below: keep video + all audio, drop every subtitle track.
# Edit MAKEMKV_SELECTION to taste; re-run install.sh to apply.
MAKEMKV_SELECTION='+sel:(video|audio),-sel:subtitle'
MAKEMKV_CONF='home/.MakeMKV/settings.conf'

echo "==> Seeding MakeMKV selection rule into $MAKEMKV_CONF"
mkdir -p "$(dirname "$MAKEMKV_CONF")"
touch "$MAKEMKV_CONF"
# Replace existing line if present, else append. Same for the Java twin
# (some MakeMKV builds read the _Java_ variant; setting both is harmless).
for key in app_DefaultSelectionString app_Java_DefaultSelectionString; do
  line="$key = \"$MAKEMKV_SELECTION\""
  if grep -q "^$key" "$MAKEMKV_CONF"; then
    # Use control-char delimiter so '|' inside the selection rule doesn't break sed.
    sed -i "s$(printf '\001')^$key.*$(printf '\001')$line$(printf '\001')" "$MAKEMKV_CONF"
  else
    printf '%s\n' "$line" >> "$MAKEMKV_CONF"
  fi
done

echo "==> Bootstrapping config/arm.yaml from template (if missing)"
if [[ ! -f config/arm.yaml ]]; then
  cp config/arm.yaml.example config/arm.yaml
  echo "  Created config/arm.yaml — REMEMBER to paste your OMDB_API_KEY into it."
fi

echo "==> Verifying NVIDIA visible inside container"
if docker info 2>/dev/null | grep -q nvidia; then
  echo "  nvidia runtime registered"
else
  echo "  WARN: nvidia runtime not registered yet — re-run after re-login if compose fails"
fi

echo "==> Pulling ARM image"
docker compose pull

cat <<EOF

Setup complete. Next steps:
  1. Find your optical drives:    lsscsi -g
     Add any extra drives to docker-compose.yml under 'devices:'
  2. Verify SMB credentials in .env
  3. Start ARM:                   docker compose up -d
  4. Open the UI:                 http://localhost:8090  (admin / password)
EOF
