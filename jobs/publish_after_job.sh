#!/bin/bash
# Called as a test job ends. Asks the login node to publish the results shortly afterward.
#
# SLURM reports the job as running until it has ended, so the tick has to run later, and
# outside the job. A detached process on the login node waits, runs one tick, and exits.
#
# This never fails the job. If it can't connect, the next scheduled tick publishes instead.

log() { echo "publish_after_job: $*"; }
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

if [ -z "${HPC_MONITOR_LOGIN_HOST:-}" ]; then
    log "no HPC_MONITOR_LOGIN_HOST, so the next scheduled tick will publish"
    exit 0
fi

# Both a signal and the normal exit can call this. mkdir lets only the first one through.
if ! mkdir "${HPC_MONITOR_RUN_DIR:?}/publish-requested" 2>/dev/null; then
    exit 0
fi

delay="${HPC_MONITOR_PUBLISH_DELAY:-120}"
tick="$(quote "${HPC_MONITOR_PYTHON:?}") $(quote "${HPC_MONITOR_REPO_DIR:?}/hpc_monitor.py")"
tick="$tick tick --results-only --site $(quote "${HPC_MONITOR_SITE_FILE:?}")"
if [ -n "${HPC_MONITOR_HOME:-}" ]; then
    tick="HPC_MONITOR_HOME=$(quote "$HPC_MONITOR_HOME") $tick"
fi
detached="sleep $delay; $tick"

if timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=20 "$HPC_MONITOR_LOGIN_HOST" \
        "nohup setsid sh -c $(quote "$detached") >/dev/null 2>&1 </dev/null &"; then
    log "asked $HPC_MONITOR_LOGIN_HOST to publish in $delay s"
else
    log "could not reach $HPC_MONITOR_LOGIN_HOST, so the next scheduled tick will publish"
fi
exit 0
