# Thin layer over the upstream ARM image: adds rsync for scripts/promote.sh.
# Doing this at build time replaces the previous runtime apt-get install in
# promote.sh, which could wedge the container if interrupted mid-install.
#
# Version pinning rationale: explicit pin (not `latest`) so every rip machine
# builds the identical image. The old 2.21.x+ syslog-ng startup breakage
# (#1663, now closed) is moot: compose replaces the syslog-ng init with a noop.
#
# MakeMKV gotcha: 1.18.x (and 2.0.0) spin at 100% CPU forever on the drive
# scan, before listing any drive, whenever a disc is loaded and
# ~/.MakeMKV has no dump_FW_*.tgz for the drive. Only 1.17.7 writes that dump,
# so a missing dump means every ARM job hangs at the first disc read. The
# dump is per drive and lives in the gitignored ./home bind mount, so each rip
# machine needs its own: run MakeMKV 1.17.7 once against the drive with a disc
# loaded and copy the dump it writes into home/.MakeMKV/. Separately, MakeMKV
# builds are time-bombed: without a current beta key (forum.makemkv.com
# f=5&t=1053; ARM scrapes it at job start) old builds exit with "This
# application version is too old", so pinning the image does not pin MakeMKV.
FROM automaticrippingmachine/automatic-ripping-machine:2.24.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync \
 && rm -rf /var/lib/apt/lists/*
