#!/bin/bash
# RHAPSODY Dragon test suite on <machine>. Submitted by hpc_monitor.py.
# Everything specific to this machine belongs here.
#
# Keep --total in run_groups.py below this time limit, so results get written.
#SBATCH --account=<account>
#SBATCH --partition=<partition>
#SBATCH --qos=<qos>
#SBATCH --nodes=2
#SBATCH --gpus-per-node=<gpus>
#SBATCH --time=00:35:00

# module load <whatever this machine needs>

set -euo pipefail

source "$HPC_MONITOR_VENV/bin/activate"

# Scale tests are off unless this is set.
export RHAPSODY_RUN_SCALE=1

python3 "$HPC_MONITOR_REPO_DIR/jobs/run_groups.py" \
    --suite "$HPC_MONITOR_SOURCE_DIR/tests/integration/test-hpc/dragon" \
    --junit "$HPC_MONITOR_RUN_DIR/junit.xml" \
    --budget 300 --total 1980 --last test_scale.py,test_mpi.py \
    -- -w slurm
