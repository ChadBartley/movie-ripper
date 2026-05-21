# Thin layer over the upstream ARM image: adds rsync for scripts/promote.sh.
# Doing this at build time replaces the previous runtime apt-get install in
# promote.sh, which could wedge the container if interrupted mid-install.
#
# Version pinning rationale: 2.21.x+ broke syslog-ng startup (upstream issue
# #1663, still unfixed in 2.23.2). 2.20.7 (Nov 2025) is the last known-good.
# Revisit `latest` once the bug is closed.
FROM automaticrippingmachine/automatic-ripping-machine:2.20.7

RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync \
 && rm -rf /var/lib/apt/lists/*
