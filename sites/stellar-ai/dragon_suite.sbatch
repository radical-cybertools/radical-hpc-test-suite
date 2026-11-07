#!/bin/bash
# RHAPSODY Dragon test suite on Stellar AI. Submitted by hpc_monitor.py.
#
# run_groups.py stops at 33 minutes, so the job can still write its results before this limit.
#SBATCH --account=csd
#SBATCH --partition=gpu
#SBATCH --qos=gpu-short
#SBATCH --nodes=2
#SBATCH --gpus-per-node=8
#SBATCH --exclusive
#SBATCH --time=00:35:00

module load openmpi/gcc/5.0.10

set -euo pipefail

# When this job ends, the login node publishes its results. See jobs/publish_after_job.sh.
export HPC_MONITOR_LOGIN_HOST=stellarai-amd
trap 'bash "$HPC_MONITOR_REPO_DIR/jobs/publish_after_job.sh" || true' EXIT
trap 'exit 143' TERM

source "$HPC_MONITOR_VENV/bin/activate"

# Scale tests are off unless this is set.
export RHAPSODY_RUN_SCALE=1

python3 "$HPC_MONITOR_REPO_DIR/jobs/run_groups.py" \
    --suite "$HPC_MONITOR_SOURCE_DIR/tests/integration/test-hpc/dragon" \
    --junit "$HPC_MONITOR_RUN_DIR/junit.xml" \
    --budget 300 --total 1980 --last test_scale.py,test_mpi.py \
    -- -w slurm -t tcp \
    --network-prefix '^(hsn|ipogif|ib|eth|en|nic|bond|lo|vmbr)\w*$'
