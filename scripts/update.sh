#!/usr/bin/env bash
# Pull latest config from this repo and rebuild ARM with the latest base image.
# `build --pull` re-fetches the pinned upstream tag and re-applies our thin
# layer (rsync). `up -d` recreates the container if the image changed.
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
