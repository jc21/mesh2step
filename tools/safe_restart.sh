#!/bin/bash
# Restart a mesh2step node only when no user can lose a result: no conversion admitted AND no finished
# result waiting for its browser (both live only in the process's memory). An idle cgroup is not enough:
# a finished, uncollected job has no process (2026-09-29: a restart at 19:00 wiped a result that had
# finished at 18:52, the user saw "unknown or expired job").
# usage: tools/safe_restart.sh [host]   (host: nativedev (default) or 100.103.234.2); run it under watchjob
set -euo pipefail
host=${1:-127.0.0.1}; [ "$host" = nativedev ] && host=127.0.0.1
tok=$(systemctl --user show mesh2step.service -p Environment | tr ' ' '\n' | sed -n 's/^MESH2STEP_ADMIN_TOKEN=//p')
busy() {
  curl -s -m 10 -H "X-Admin-Token: $tok" "http://$host:8000/api/admin/stats" |
    python3 -c 'import json,sys; n=json.load(sys.stdin)["nodes"][0]; print(n["pending_jobs"] + n["admitted"])'
}
while [ "$(busy)" != 0 ]; do sleep 20; done     # waiting results expire after RESULT_TTL_S (1 h) at worst
if [ "$host" = 127.0.0.1 ]; then
  systemctl --user restart mesh2step.service
else
  ssh "$host" 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart mesh2step.service'
fi
echo "restarted $host"
