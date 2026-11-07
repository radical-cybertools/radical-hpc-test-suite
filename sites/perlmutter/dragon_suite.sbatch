#!/bin/bash
# RHAPSODY Dragon test suite on NERSC Perlmutter. Submitted by hpc_monitor.py.
#
# GPU nodes are chosen by constraint, not partition. The regular QOS gives whole nodes.
# The debug QOS stops at 30 minutes, which is too short for this job.
# run_groups.py stops at 33 minutes, so the job can still write its results before this limit.
#SBATCH --account=amsc007_g
#SBATCH --constraint=gpu
#SBATCH --qos=regular
#SBATCH --nodes=2
#SBATCH --gpus-per-node=4
#SBATCH --time=00:35:00

module load PrgEnv-gnu cray-mpich cudatoolkit craype-accel-nvidia80

set -euo pipefail

# When this job ends, the login node publishes its results. See jobs/publish_after_job.sh.
export HPC_MONITOR_LOGIN_HOST=perlmutter.nersc.gov
trap 'bash "$HPC_MONITOR_REPO_DIR/jobs/publish_after_job.sh" || true' EXIT
trap 'exit 143' TERM

source "$HPC_MONITOR_VENV/bin/activate"

# RHAPSODY's Perlmutter guide requires this. It only changes this run's venv.
dragon-config add --ofi-build-lib=/opt/cray/libfabric/default/lib64
dragon-config add --ofi-include=/opt/cray/libfabric/default/include
dragon-config add --ofi-runtime-lib=/opt/cray/libfabric/default/lib64

# Scale tests are off unless this is set.
export RHAPSODY_RUN_SCALE=1

# -t hsta makes a broken setup fail, where the default would quietly fall back to TCP.
python3 "$HPC_MONITOR_REPO_DIR/jobs/run_groups.py" \
    --suite "$HPC_MONITOR_SOURCE_DIR/tests/integration/test-hpc/dragon" \
    --junit "$HPC_MONITOR_RUN_DIR/junit.xml" \
    --budget 300 --total 1980 --last test_scale.py,test_mpi.py \
    -- -w slurm -t hsta
