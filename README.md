# RADICAL HPC test suite monitor

A central public view of RHAPSODY test runs across our HPC machines, published to [CDash](https://my.cdash.org/index.php?project=RADICAL-HPC-Test-Suite), with results archived in this repo.

**Status: tested on real jobs on Stellar AI, reporting to the sandbox project. Not live yet.**

- [Spec](docs/spec.md): the problem and constraints
- [Assessment](docs/assessment.md): the options compared and why CDash was chosen
- [Spike results](spike/RESULTS.md): how CDash actually behaves, and the design that came from it
- [Machine setup](sites/README.md): the job-script contract and how to add a machine

## How it works

A short scheduled command (`tick`) runs every 15 minutes on each machine. It checks PyPI for new releases once a day, submits the test suite, follows the job with `sacct`, and reports to CDash. No process stays running between ticks.

| Job | Dashboard row |
|---|---|
| Queued | Version shown, test cells blank |
| Running | Orange Not Run cell |
| Passed | Green |
| Failed or killed by SLURM | Red, with the reason on the `slurm-job` test |

Results normally appear within 15 minutes of a job ending, and up to about 30 if the row was showing Running. A job shorter than 15 minutes may never show Running. CDash has no real running state and its colours are fixed, so orange is the closest match to yellow.

## Reading a result

Click a row's test counts, then the `slurm-job` test. It shows the verdict, SLURM job ID, full version, Dragon version, source commit, exit code, nodes, skipped tests, and the end of the job log. A failed test named `<group>.launcher` means that test group crashed or ran out of time, so its other results are lost; later groups still report. The dashboard row itself shows only the first six characters of the version.

## Commands

```bash
# Scheduled command; safe to run by hand
python3 hpc_monitor.py tick --site sites/stellar-ai/site.toml

# Run a chosen version now: a baseline or a rerun. A rerun gets a new row.
python3 hpc_monitor.py run --site sites/stellar-ai/site.toml --version 0.6.0

# Preview either command; changes nothing
python3 hpc_monitor.py run --site sites/stellar-ai/site.toml --version 0.6.0 --dry-run
```

It needs Python 3.11 or newer and has no other dependencies.

Run the tests with `python3 -m unittest discover -s tests`. They're offline, using fake SLURM commands and a local stand-in for CDash.

## Files on each machine

Under `~/.hpc-monitor/`:

| Path | Contents |
|---|---|
| `<machine>/state.json` | Progress of every run |
| `<machine>/monitor.log` | What each tick did; look here first |
| `<machine>/runs/<run>/` | The original `junit.xml` and `slurm.out` |
| `<machine>/archive/` | The monitor's clone of the `results` branch |
| `cdash.token` | Submission token (`chmod 600`) |
| `deploy_key` | GitHub key for the archive (`chmod 600`) |

Logs are scrubbed of usernames, home paths, and secret-looking values before they leave the machine. The scrubbing is pattern-based, so don't print secrets in job output.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Machine stopped updating, orange stuck | The schedule or SSH stopped working | Check `~/.hpc-monitor-scron.out` and `scrontab -l`, and run `tick` by hand |
| Row shows both running and final tests | An update arrived before CDash processed `Done.xml` | The monitor repairs this by itself after about 30 minutes; see `monitor.log` |
| Two rows for one run | Repair failed, so the result went to a new row | The newer row is correct. Delete the old one on CDash |
| Red: `could not start the job` | `pip`, the tag checkout, or `sbatch` failed | See the `slurm-job` test. After fixing, rerun with `run --version` |
| Red: `job completed but no results` | The job script didn't write `junit.xml` | Check `slurm.out` in the run directory |
| New release isn't running | Another run is open, or the daily check hasn't come round | Check `state.json` and `monitor.log` |

## Things to know

- **Running from RHAPSODY `main`:** until a release pins Dragon 0.15.0, code and tests come from one `main` commit. Rows show the version `main` reports, such as `0.6.0`, even though this isn't that release; the `slurm-job` test has the commit. If `main` changes its version, or PyPI publishes a new release, runs go red with `could not start the job` until `site.toml` is updated.
- **Shared environment:** the monitor upgrades the machine's Python environment in place on a new release. The previous version isn't restored, and using that environment by hand means it can change under you. If `pip` fails partway, the environment may need repairing by hand.
- **One run at a time:** runs on a machine share that environment, so a new one waits until the open one closes.
- **First release seen:** the monitor records the current release without running it, since a run uses allocation. Use `run --version` for a baseline.
- **History:** my.cdash.org publishes no limits. About 500 builds per project is inferred, with the oldest deleted first, and retention must be raised from its 90-day default. The `results` branch here is the durable record.
