#!/bin/bash
# Which scheduling mode can this machine use? Prints one PASS or FAIL line per check.
# Output decides "direct" or "ssh" in sites/<machine>/scrontab. See spike/RESULTS.md.
#
# Stellar AI:  sbatch spike/probe_network.sbatch
# Elsewhere:   override on the command line, e.g.
#              sbatch -A <account> -p <partition> -q <qos> spike/probe_network.sbatch
#
#SBATCH --job-name=hpc-monitor-probe
#SBATCH --account=csd
#SBATCH --partition=cpu
#SBATCH --qos=test
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --time=00:05:00

# Checks are expected to fail on some machines, so don't stop on errors.
set -u

check() {
    local label=$1
    shift
    if output=$("$@" 2>&1); then
        echo "PASS  $label"
    else
        echo "FAIL  $label  ($(echo "$output" | tail -n 1 | cut -c1-120))"
    fi
}

echo "node:        $(hostname -f)"
echo "submit host: ${SLURM_SUBMIT_HOST:-unknown}"

echo "--- direct internet from this node"
check "my.cdash.org" curl -sSf -m 15 -o /dev/null https://my.cdash.org/
check "github.com"   curl -sSf -m 15 -o /dev/null https://github.com/
check "pypi.org"     curl -sSf -m 15 -o /dev/null https://pypi.org/simple/rhapsody-py/

echo "--- ssh back to the login node (read-only: runs hostname)"
check "ssh ${SLURM_SUBMIT_HOST:-unknown}" ssh \
    -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${SLURM_SUBMIT_HOST:-localhost}" hostname

short_host=${SLURM_SUBMIT_HOST%%.*}
if [ "$short_host" != "${SLURM_SUBMIT_HOST:-}" ]; then
    check "ssh $short_host" ssh \
        -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "$short_host" hostname
fi

echo "--- internet through a site proxy module, if the machine has one"
if type module >/dev/null 2>&1 && module avail proxy 2>&1 | grep -q "proxy/"; then
    # No pipeline here: a piped module command would run in a subshell and change nothing.
    module load proxy/default >/dev/null 2>&1
    echo "proxy variables set: $(env | grep -ciE '^(https?|all)_proxy=')"
    check "proxy my.cdash.org" curl -sSf -m 15 -o /dev/null https://my.cdash.org/
    check "proxy github.com"   curl -sSf -m 15 -o /dev/null https://github.com/
    check "proxy pypi.org"     curl -sSf -m 15 -o /dev/null https://pypi.org/simple/rhapsody-py/
    module unload proxy/default >/dev/null 2>&1
else
    echo "no proxy module found"
fi

echo "--- state directory and locking in \$HOME"
probe_dir=$(mktemp -d "$HOME/.hpc-monitor-probe.XXXXXX")
check "flock"       flock -n "$probe_dir/lock" true
check "fcntl lock"  python3 -c "import fcntl,sys; fcntl.flock(open(sys.argv[1],'w'),fcntl.LOCK_EX|fcntl.LOCK_NB)" "$probe_dir/lock"
rm -rf "$probe_dir"

echo "--- tools"
for tool in sacct sbatch scrontab git curl python3; do
    check "$tool" command -v "$tool"
done
python3 --version
