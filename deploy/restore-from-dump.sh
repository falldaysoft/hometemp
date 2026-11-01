#!/bin/bash
# Restore a pg_dump custom-format file into the VM's shared postgres container
# as the `hometemp` role. Stops the app during the restore so nothing
# writes mid-way.
# Usage (on the VM): ~/apps/hometemp/restore-from-dump.sh ~/backups/postgres/<file>.dump
set -euo pipefail
DUMP="${1:?usage: restore-from-dump.sh <dump file>}"
cd /home/ubuntu/apps/hometemp
docker compose stop
docker exec -i postgres pg_restore -U hometemp -d hometemp --clean --if-exists --no-owner --no-acl < "$DUMP"
docker compose start
echo "restored $DUMP"
