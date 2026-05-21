#!/bin/sh
# Mounted over /etc/my_init.d/10_syslog-ng.init inside the ARM container to
# bypass the syslog-ng startup failure on Arch/Manjaro Docker hosts.
# See README "Known issues" for context. ARM itself logs via Python to
# /home/arm/logs/, so disabling syslog-ng has no effect on functionality.
exit 0
