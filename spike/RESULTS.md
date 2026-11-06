# Spike results

Run on 2026-11-05 against the `RADICAL-HPC-Test-Suite-Sandbox` project on my.cdash.org (CDash v5.5.2-86), and on Stellar AI. All builds are synthetic and named `spike-*`. Their start times are made up, because the spike hardcodes them in the build stamps.

## Agreed design

| State | Upload | What the dashboard row shows |
|---|---|---|
| Pending | `Configure.xml` (records the `sbatch` submission) and `Update.xml` (version), no `Done.xml` | Row exists, version shown, test cells blank |
| Running | `Test.xml` with a `notrun` test, then `Done.xml` once the row shows it | Orange Not Run cell |
| Final | `Update.xml`, then `Test.xml` once applied, then `Done.xml` | Green or red; earlier entries gone |

A job that finishes between checks skips the running state and needs no replacement.

**Other decisions**
- The monitor runs every 15 minutes and makes at most one state change per run, which leaves a safe gap between row updates.
- Each update is read back through the API. If the row is wrong, the next run sends `Done.xml`, waits, and posts the result again.
- Results are sent as `Test.xml`, converted from JUnit. Original JUnit is archived in git.
- The build name stays stable (`rhapsody-dragon`), so release differences work. The row shows up to six characters of the version.
- Stellar AI uses SSH mode: a `scrontab` job runs `ssh stellarai-amd` and the monitor runs on the login node.

## The four main limitations

| Limitation | Status |
|---|---|
| No real pending or running state | **Reduced, not removed.** Blank test cells for pending. Orange Not Run for running is accurate: those tests haven't run. Literal yellow isn't possible. |
| Pending and running look alike | **Fixed and tested** (`spike-distinct`, `spike-protocol`). |
| Row updates depend on timing | **Reduced.** One replacement per run instead of two, a 15-minute gap, a read-back after each update, and a tested repair. |
| Relying on CDash internals | **Reduced.** CDash's own test suite covers replacement (`test_replacebuild.php`, `DoneXMLTest.php`) and `ctest` itself sends `Done.xml`, but I found no documentation promising this behaviour. The read-back would detect a change. |

## CDash checks

| # | Question | Result |
|---|---|---|
| 1a | Placeholder + Done, then final + Done (`spike-two-step`) | **Works.** One row, no orange left. |
| 1b | Three steps, Done sent immediately (`spike-replace`, `spike-replace-slow`) | **Fails.** Final is added next to the running entry. |
| 1c | Three steps, Done after a fixed 40s wait (`spike-three-step`) | **Worked, but slowly.** The row still showed `RUNNING` about two minutes after the final upload, and was correct when next read about 12 minutes later. |
| 1d | Three steps, Done once the row updates, 2-minute gap (`spike-twice-a`) | **Works.** One clean row. |
| 1e | Same, 30-second gap (`spike-twice-b`) | **Fails.** Running was added next to pending. Done is processed through a queue, and we can't see from outside when that has happened. |
| 1f | Repair of the broken row from 1e: Done, wait 5 minutes, final | **Works.** One clean row, 16 seconds after the upload. |
| 1g | Pending in Configure, running in Not Run, one replacement (`spike-distinct`) | **Works.** The "Warning:" text in its log wasn't counted, so no fake warning is involved. |
| 1h | Complete agreed design, with version (`spike-protocol`) | **Works** with 3-minute gaps. Version shown at every state. |
| 2 | Without Done (`spike-nodone`) | Results are added, not replaced. **Done is required for replacement.** |
| 3a | Stable name across releases (`spike-stable`) | **Works.** v1.1.0 shows one more failure and one fewer pass than v1.0.0. |
| 3b | Version in the Update revision | **Partly.** Shown on the row, cut to six characters (`0.10.12` shows as `0.10.1`). Full value stored. |
| 3c | Custom `Release` track | Stored as the build type, but the row appears under Experimental. |
| 3d | `SLURM Job ID` measurement | Stored on every test. Display not checked in a browser. |
| 4a | Unchanged pytest JUnit (`spike-junit`, `spike-j0-unchanged`) | **Fails.** Empty row. pytest omits the `type` attribute the parser reads. |
| 4b | `type` added, no wrapper (`spike-j1-type`) | Parses, but into a separate row with an auto-generated stamp. |
| 4c | `<Site>` wrapper, no `type` (`spike-j3-site-notype`) | **Fails.** Empty row. |
| 4d | `type` added plus `<Site>` wrapper (`spike-j2-site`) | **Works**, but skipped tests count as passed and `<error>` text is lost. |
| 5 | 2 MiB test output (`spike-large`) | Stored in full. We set our own trim. |
| 6 | Default landing page, no recent builds | **Fine.** Opens on the most recent date with builds. |
| 7 | Expected builds flag a silent machine | **Not tested.** Needs project admin; to do during go-live. |
| 8 | Test labels (`spike-label`) | Stored on tests, but don't reach the row. |

### Things to carry into the monitor

- Processing time varied from about 15 seconds to over 4 minutes, and in 1c it took over 2 minutes. Never assume an upload has been applied; read it back.
- During the final step the row is briefly blank, between `Update.xml` and `Test.xml`. With 15-minute runs this should last a few minutes at most.
- Use the real `sbatch` time in the build stamp, and the real job start from `sacct` for the tests.
- A rerun of a release gets a new stamp.

### Not yet proven

- The 15-minute schedule. The sequence ran with 3-minute gaps here; longer gaps should be safer, but real timing comes in the end-to-end phase.
- A job that goes straight from pending to final. Adding `Test.xml` to a Configure row worked in the running step, but this exact path hasn't run.
- One run of each sequence isn't proof of reliability, which is why the read-back and repair are part of the design.

## Scheduling probe

Each machine needs at least one working mode. "Direct" needs all three sites reachable from a compute node. "SSH" needs only the connection back to the login node.

| Machine | `crontab` | `scrontab` | Compute internet | SSH to login | Lock in `$HOME` | Mode |
|---|---|---|---|---|---|---|
| Stellar AI | Denied | Enabled | No (no DNS) | **Yes**, short name `stellarai-amd` only; no key setup needed | Works | **SSH** |
| Perlmutter | | | | | | |
| Bridges-2 | | | | | | |
| Delta | | | | | | |

**Stellar AI notes**
- The probe ran as a normal `sbatch` job on `sai-n04n1`. A real `scrontab` entry is still to be confirmed in the end-to-end phase.
- The full name `stellarai-amd.princeton.edu` does not resolve from compute nodes.
- The `proxy/default` module was not tested properly: the probe loaded it in a pipeline, so it had no effect. That line is now fixed. It isn't needed here, and the module says it only supports a pre-approved list of servers.
- The probe confirms tools are installed, not that `sacct` and `sbatch` work from a compute node. In SSH mode they run on the login node.

## Phase 4: real jobs on Stellar AI

All runs reported to the sandbox project on 2026-11-05 and 06, using real SLURM jobs and my.cdash.org.

| Row | Job | SLURM result | Dashboard | Path tested |
|---|---|---|---|---|
| `rhapsody-dragon` | 14134 | `COMPLETED`, 49 s, after a 5 h 20 min queue | 15 passed | Real suite, 15-minute schedule, pending to final |
| `timed-suite` | 15094 | `COMPLETED`, 22 min | 2 passed | Default timings, 15-minute schedule, pending to running to final |
| `forced-timeout` | 14139 | `TIMEOUT` | 1 failed | Running to final, short timings |
| `forced-cancel` | 14167 | `CANCELLED` | 1 failed | Running to final, short timings |
| `forced-broken` | 14196 | One failed test | 2 passed, 1 failed | Running to final, short timings |
| `forced-nojunit` | 14224 | `COMPLETED`, no JUnit | 1 failed | Pending to final, short timings |

Counts include the `slurm-job` test.

**Confirmed**
- `dragon -w slurm -t tcp ... python3 -m pytest` works under `sbatch` on 2 GPU nodes (16 GPUs), with `RHAPSODY_HPC=1` and the suite on `PYTHONPATH`.
- The `scrontab` to `ssh stellarai-amd` route ran every 15 minutes overnight with no missed ticks in the log.
- Replacement at the default timings left one row with one result for each test, and no repairs were needed. `timed-suite` ended 03:53:49, and its result was complete at 04:00:48.
- `kill -9` on a tick just after a `Test.xml` upload caused no duplicate and no repair. The next scheduled tick carried on. The state had already been saved when the kill landed, so a kill between upload and save is covered only by the unit tests.
- `env-rhapsody-015` gained only `pytest`, `pytest-asyncio`, and their three dependencies.

**Suite behaviour on Stellar**
- 14 passed and 10 skipped: 6 MPI (`mpi4py` isn't installed) and 4 scale (`RHAPSODY_RUN_SCALE` is unset).
- `test_throughput.py` has no pytest tests, so pytest collects nothing from it.
- `env-rhapsody-015` holds a local build (`v0.6.0-56-g142f396`) reporting 0.6.0, while the tests come from tag `v0.6.0`. Job 14134 is therefore not a true 0.6.0 baseline.

**Consequences**
- A 49-second job is caught running by about 5% of 15-minute checks, so most rows go straight from pending to final.
- A row's duration on CDash includes queue time. The job's run time is on the `slurm-job` test.
- A result normally appears within 15 minutes of the job ending, and up to about 30 if the row was showing Running.

### Released 0.6.0 with MPI and scale

Both ran on 2026-11-06 in `env-rhapsody-060`: `rhapsody-py` 0.6.0 from PyPI, `dragonhpc` 0.14.1, and `mpi4py` 4.1.2 built against `openmpi/gcc/5.0.10`.

| Row | Job | SLURM result | Dashboard |
|---|---|---|---|
| `rhapsody-dragon` (MPI, no scale) | 15685 | `FAILED` after 10 min 33 s | 1 failed: `SLURM ended the job: FAILED; JUnit file is missing` |
| `rhapsody-dragon-scale` | 15786 | `COMPLETED` in 2 min 6 s | 5 passed (4 tests, 107 s, plus `slurm-job`) |

- The first four MPI tests failed. During the fifth, a Dragon step was cancelled and the launcher exited, so pytest never wrote JUnit.
- The 14 non-MPI tests ordered after `test_mpi.py` never ran, so they remain unproven on Dragon 0.14.1.
- The log shows Slurm `mpi/pmix_v5` collective timeouts. The cause isn't established, and the per-test failure text was lost with the JUnit.
- The monitor reported this correctly as a red row with the reason. The limitation is that a launcher crash loses all per-test results for that job.

### Test groups, from RHAPSODY's refactor branch

Tests now run as four groups through RHAPSODY's `run_suite.py`, in `env-rhapsody-060` with code and tests from one commit and Dragon 0.15.0.

**Real run:** job 15895, commit `d8b83a74be17`, 2 GPU nodes, 6 min 5 s. CDash row: 14 passed, 1 failed (13 tests plus `slurm-job` passed).

| Group | Time | Result |
|---|---|---|
| `tasks` | 25 s | 4 passed |
| `placement` | 26 s | 5 passed, including the exact-GPU checks |
| `scale` | 139 s | 4 passed |
| `mpi` | 172 s | `test_mpi_gather_single_node` failed: `Timeout after 120.0s: 0/1 tasks completed`. The other 5 didn't run, because of `--maxfail=1` |

**Dragon restart in one allocation** (CPU nodes)
- Job 15852: a launch that was stopped left its `dragon-backend` SLURM step running, which held the memory and blocked the next launch.
- Job 15881: after cancelling leftover steps and running `srun ... dragon-cleanup -s`, Dragon restarted all three times. The runner now does this.
- `dragon` returned 0 when the application exited 1 and when it was killed, so the runner treats missing results as the sign of a crash.

**Forced failures** (CPU nodes, jobs 15901 and 15910)
- A killed pytest gave `placement.launcher` failed, and the next group still started.
- A very short budget gave `tasks.launcher` failed, and the next group still started. Pytest finished during the stop grace period, so that group's results were kept.
- `RHAPSODY_HPC_REQUIRE=gpu` on nodes without GPUs failed the 3 GPU tests instead of skipping them.
- `scale` doesn't finish on 2 CPU nodes with 16 cores each. It exceeded its budget with or without an earlier crash, so that allocation is the likely cause, not the crash. An over-budget group that pytest doesn't finish loses its per-test results.
- The first attempt (job 15897) found that `node.gpus` is `None` on CPU-only nodes, which failed every test. That's fixed in commit `6f36883`, which changes nothing on GPU nodes, so job 15895 wasn't rerun.

**Not tested on real jobs:** the Dragon version check, which is covered only by unit tests, and a crash on GPU nodes.

## Rerun

```bash
python3 spike/spike.py --project <sandbox> --report-only          # reads only
python3 spike/spike.py --project <sandbox> --check-protocol 180   # submits builds
sbatch --output=$PWD/spike/slurm-probe-%j.out spike/probe_network.sbatch
```

Point `--output` at shared storage: `/tmp` is local to the compute node, so output sent there doesn't come back.
