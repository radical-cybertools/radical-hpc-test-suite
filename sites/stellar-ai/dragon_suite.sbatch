#!/bin/bash
# RHAPSODY Dragon test suite on Stellar AI. Submitted by hpc_monitor.py.
# The contract with the monitor is in sites/README.md.
#
# The time limit fits the runner's group budgets, which total 14 minutes.
#SBATCH --account=csd
#SBATCH --partition=gpu
#SBATCH --qos=gpu-short
#SBATCH --nodes=2
#SBATCH --gpus-per-node=8
#SBATCH --exclusive
#SBATCH --time=00:15:00

module load openmpi/gcc/5.0.10

set -euo pipefail

source "$HPC_MONITOR_VENV/bin/activate"

# A capability listed here must be present: if it's missing, its tests fail instead of skipping.
export RHAPSODY_HPC_REQUIRE=gpu,multi_node,mpi

python3 "$HPC_MONITOR_SOURCE_DIR/tests/integration/test-hpc/run_suite.py" \
    --backend dragon --junit "$HPC_MONITOR_RUN_DIR/junit.xml" \
    -- -w slurm -t tcp \
    --network-prefix '^(hsn|ipogif|ib|eth|en|nic|bond|lo|vmbr)\w*$'
