# Machine setup

Each machine has its own directory here. Nothing about a specific machine belongs in `hpc_monitor.py`.

| File | Purpose |
|---|---|
| `site.toml` | What the monitor needs: machine name, Python environment, CDash project, and what to run |
| `dragon_suite.sbatch` | A complete, ordinary sbatch script, owned by this machine |
| `scrontab` | The monitor's 15-minute schedule |

## Job-script contract

The monitor never generates or edits a job script. It depends only on this:

| Direction | Contract |
|---|---|
| Monitor sets | `HPC_MONITOR_RUN_DIR`, `HPC_MONITOR_VENV`, `HPC_MONITOR_SOURCE_DIR`, `HPC_MONITOR_VERSION`, `HPC_MONITOR_REPO_DIR` |
| Monitor overrides | `--job-name` and `--output` (`$HPC_MONITOR_RUN_DIR/slurm.out`), on the `sbatch` command line |
| Script must | Write `$HPC_MONITOR_RUN_DIR/junit.xml` |
| Monitor reads | The job ID from `sbatch`, the state from `sacct`, then `junit.xml` and `slurm.out` |

Machine-specific settings all go in the script: `#SBATCH` directives, modules, Dragon network settings, and `srun` flags. Scripts normally call RHAPSODY's `tests/integration/test-hpc/run_suite.py`, which runs each test group under its own launcher, and pass the `dragon` flags after `--`. Scripts can replace it, as long as `junit.xml` gets written.

Set `RHAPSODY_HPC_REQUIRE` to the capabilities the machine should have, such as `gpu,multi_node,mpi`. If one is missing, its tests fail instead of skipping.

A script that exits cleanly without writing `junit.xml` shows as a failure on the dashboard.

## Adding a machine

1. Run the probe, and record the results in `spike/RESULTS.md`. Set account, partition, and QOS for that machine on the command line:
   ```bash
   sbatch -A <account> -p <partition> -q <qos> --output=$PWD/spike/slurm-probe-%j.out spike/probe_network.sbatch
   ```
2. Copy `_template/` to `sites/<machine>/` and fill it in.
3. Check the script without allocating anything, using the variables it expects:
   ```bash
   sbatch --test-only sites/<machine>/dragon_suite.sbatch
   ```
4. Preview what the monitor would do:
   ```bash
   python3 hpc_monitor.py run --site sites/<machine>/site.toml --version <version> --dry-run
   ```
5. Run a baseline with the same command without `--dry-run`, then install the schedule with `scrontab sites/<machine>/scrontab`.

The monitor needs Python 3.11 or newer where it runs. If the system Python is older, use the machine's environment Python in the `scrontab` line.

## More than one run on a machine

Add another `[[run]]` entry to `site.toml`, with its own `name` and `job_script`. Each `name` becomes its own dashboard row.

These optional `[[run]]` settings have defaults for RHAPSODY:

| Setting | Default |
|---|---|
| `install` | `<package>[dragon]=={version}`. Can use `{source_dir}` to install from the cloned source, which always reinstalls |
| `extra_packages` | `["pytest", "pytest-asyncio"]` |
| `record_versions` | `["dragonhpc"]` |
| `require_versions` | None. A table such as `{ dragonhpc = "0.15.0" }`: a different installed version gives a red row and submits nothing |
| `source_repo` | The RHAPSODY repository |
| `source_tag` | `v{version}` |

Runs on a machine share one Python environment, so they go one after another.
