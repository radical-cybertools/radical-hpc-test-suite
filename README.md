# RADICAL HPC test suite monitor (Grafana)

Tests each new RHAPSODY release once a month on our HPC machines, and publishes the results to the `results` branch of this repo, which a public Grafana dashboard reads.

## How it works

```
Login node: tick, about every 15 min, then exits
  1. Once a month: ask PyPI for the latest release
  2. New release: build a fresh venv, clone tests from the tag, sbatch
  3. Follow the job with sacct
  4. Rebuild the files in data/ and push them if anything changed
                     |
                     v
"results" branch  <-- Grafana (Infinity plugin) <-- viewers, no login
```

The compute job never talks to the outside world. Jobs killed by SLURM can't report themselves, so the tick reports them from `sacct`.

| Job | Dashboard |
|---|---|
| Queued | Yellow `PENDING` |
| Running | Yellow `RUNNING` |
| Passed | Green `PASSED` |
| Failed, killed by SLURM, or couldn't start | Red `FAILED`, with the reason |

A status change appears within about 25 minutes: up to 15 for the next tick, about 5 for GitHub's raw file cache, and up to 5 for the dashboard's refresh. So a job that has started can still show `PENDING` for a while, and a job shorter than a tick may never show `RUNNING`.

## Finding out why a run failed

| Where | What it shows |
|---|---|
| "why it failed" column | One line: the first failed test and how many more |
| The machine's section | One row per run, with its results and resource use; click its arrow for every test that ran, with its result, time, and any error or skip reason, failures first |
| `logs` link | The run's folder: tracebacks in `junit.xml`, job output in `log_tail.txt` and a log per failed file, plus `summary.json` |

**Resource use** comes from `sacct` once a job ends: CPU use as a share of the allocation, and peak memory as the largest single task. GPU use isn't shown, because SLURM reported 0 for it on the steps checked. Per-file numbers match SLURM steps to each file's log times, so treat them as close, not exact.

**If a machine stops reporting,** its rows keep their last status. Check "Last report" in its section: an idle machine updates that once a day.

## Which releases get tested

On the first tick of each calendar month, the monitor reads the latest version from PyPI and tests it if it hasn't been tested before.

- At most one release per month is tested, and always the newest. Releases in between are skipped.
- A release published after the check waits for next month's check. Change this with `release_check_day` under `[timing]`.
- The first time a machine sees a release, it only records it, because a run spends allocation. Use `run` for a baseline.
- If PyPI can't be reached, the next tick tries again.

Each run installs `rhapsody-py[dragon]==<version>` from PyPI in its own venv, with the Dragon version that release pins, and takes its tests from tag `v<version>`. The venv and source clone are deleted once results are published.

## Commands

```bash
# Scheduled command; safe to run by hand
python3 hpc_monitor.py tick --site sites/stellar-ai/site.toml

# Test a chosen version now: a baseline or a rerun. A rerun gets a new row.
python3 hpc_monitor.py run --site sites/stellar-ai/site.toml --version 0.6.0

# Preview either command; changes nothing
python3 hpc_monitor.py run --site sites/stellar-ai/site.toml --version 0.6.0 --dry-run
```

It needs Python 3.11 or newer and has no other dependencies. Run the tests with `python3 -m unittest discover -s tests`. They're offline, using fake SLURM commands and a local git repo.

## What gets published

Everything goes in `data/` on the `results` branch. That branch holds only data, so machine commits stay out of the code history and the dashboard's URLs don't depend on any code branch. Each machine writes only its own files.

| Path | Contents |
|---|---|
| `data/<machine>.csv` | One row per run |
| `data/<machine>-tests.csv` | One row per test in each finished run, with its result, time, and any error or skip reason |
| `data/<machine>-usage.csv` | CPU and peak memory per test file, from `sacct` |
| `data/runs/<machine>/<software>/<version>/<job>/` | `junit.xml`, `log_tail.txt`, `<file>.log` for each failed file, `summary.json` |

Everything published is public. Logs are scrubbed of usernames, home paths, and secret-looking values, but the scrubbing is pattern-based, so don't print secrets in job output.

## Files on each machine

Under `~/.hpc-monitor-grafana/`:

| Path | Contents |
|---|---|
| `<machine>/state.json` | Progress of every run |
| `<machine>/monitor.log` | What each tick did; look here first |
| `<machine>/runs/<run>/` | Original `junit.xml`, `slurm.out`, and per-file logs in `groups/` |
| `<machine>/data-repo/` | The monitor's clone of the `results` branch |
| `deploy_key` | GitHub key for this repo (`chmod 600`) |

## The job script

Each machine owns its sbatch script. The monitor depends only on this:

| Direction | Contract |
|---|---|
| Monitor sets | `HPC_MONITOR_RUN_DIR`, `HPC_MONITOR_VENV`, `HPC_MONITOR_SOURCE_DIR`, `HPC_MONITOR_VERSION`, `HPC_MONITOR_REPO_DIR` |
| Monitor overrides | `--job-name` and `--output`, on the `sbatch` command line |
| Script must | Write `$HPC_MONITOR_RUN_DIR/junit.xml` |

`jobs/run_groups.py` runs each file in `tests/integration/test-hpc/dragon` under its own launcher and time budget, so a hang costs that file and not the whole run. `jobs/pytest_live.py` records each result as it happens, so a stopped file keeps the tests and errors it already had. `--total` must stay below the job's time limit.

The schedule and the jobs run the code from this checkout, so keep it on this branch while a schedule is installed.

## Adding a machine

Follow [docs/adding-a-machine.md](docs/adding-a-machine.md).

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| "Last report" is days old | Schedule, SSH, or push stopped working | Check `monitor.log` and `scrontab -l`, and run `tick` by hand |
| Red: `Could not start the job` | `pip`, the tag checkout, or `sbatch` failed | Fix it, then rerun with `run --version` |
| Red: `job completed but no results` | The job script didn't write `junit.xml` | Check `slurm.out` in the run directory |
| Red: `<file>.launcher` | A test file crashed its launcher or ran over budget | See `groups/<file>.log` in the run directory |
| New release isn't running | The month's check already happened | Wait for next month, or use `run --version` |
