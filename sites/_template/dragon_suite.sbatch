#!/bin/bash
# RHAPSODY Dragon test suite on <machine>. Submitted by hpc_monitor.py.
# Everything specific to this machine belongs here. See sites/README.md.
#SBATCH --account=<account>
#SBATCH --partition=<partition>
#SBATCH --qos=<qos>
#SBATCH --nodes=2
#SBATCH --gpus-per-node=<gpus>
#SBATCH --time=00:15:00

set -euo pipefail

# module load <whatever this machine needs>
source "$HPC_MONITOR_VENV/bin/activate"

# A capability listed here must be present: if it's missing, its tests fail instead of skipping.
export RHAPSODY_HPC_REQUIRE=gpu,multi_node,mpi

python3 "$HPC_MONITOR_SOURCE_DIR/tests/integration/test-hpc/run_suite.py" \
    --backend dragon --junit "$HPC_MONITOR_RUN_DIR/junit.xml" \
    -- -w slurm
