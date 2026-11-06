# Adding a new machine

This guide connects an HPC machine to the monitor. When you finish, the machine tests each new RHAPSODY release once a month and shows its results in its own section of the public dashboard.

Plan for about an hour of setup, plus however long the first job waits in the queue.

**Tested so far:** only Stellar AI, using the SSH schedule in [step 8](#step-8-install-the-schedule). Other machines may need small changes, and the direct and cron schedules haven't been run yet.

In the commands below, replace:

| Placeholder | Meaning | Example |
|---|---|---|
| `<machine>` | Lowercase name for the data files and folders | `delta` |
| `<Display Name>` | Name shown on the dashboard | `Delta` |
| `<repo>` | Full path to your clone | `/home/me/radical-hpc-test-suite` |

## What you need

- A login on the machine, and an allocation you're allowed to spend on test jobs. Each run uses 2 nodes for up to 35 minutes.
- Admin rights on the GitHub repo, to add a deploy key.
- A GPU partition. The tests in `v0.6.0` fail during setup on nodes without GPUs. This was fixed after that release.

## Step 1: Check the machine

Run these on a login node:

```bash
python3 --version                   # must be 3.11 to 3.13
sbatch --version && sacct --version
curl -sI https://pypi.org/pypi/rhapsody-py/json | head -1     # needs a 200
ssh -T git@github.com               # an authentication message means it's reachable
```

**If `python3` is older than 3.11,** load or install a newer one, and note its full path from `which python3`. Scheduled ticks don't load modules, so you'll use that full path in steps 4 and 8.

**If `ssh` to GitHub is blocked,** the monitor can't publish from this machine as configured. Ask the site whether outbound SSH is allowed before going further.

## Step 2: Clone the repo

```bash
cd ~
git clone --branch feature/grafana-approach \
    https://github.com/radical-cybertools/radical-hpc-test-suite.git
cd radical-hpc-test-suite
python3 -m unittest discover -s tests     # offline, and uses no allocation
```

Use `main` in place of `feature/grafana-approach` once that branch is merged.

Keep this folder in place and on this branch. The schedule and the jobs run the code from here.

## Step 3: Add a deploy key

Each machine gets its own key, so you can revoke one without affecting the others.

```bash
mkdir -p ~/.hpc-monitor-grafana
ssh-keygen -t ed25519 -N "" -C "hpc-monitor-<machine>" -f ~/.hpc-monitor-grafana/deploy_key
chmod 600 ~/.hpc-monitor-grafana/deploy_key
cat ~/.hpc-monitor-grafana/deploy_key.pub
```

On GitHub, open the repo's **Settings → Deploy keys → Add deploy key**:

1. Set **Title** to `hpc-monitor-<machine>`.
2. Paste the `.pub` line into **Key**. Never paste or commit the private `deploy_key` file.
3. Tick **Allow write access**.
4. Click **Add key**.

Then test it:

```bash
ssh -i ~/.hpc-monitor-grafana/deploy_key -o IdentitiesOnly=yes -T git@github.com
```

Success says `Hi radical-cybertools/radical-hpc-test-suite!`, and the exit code is 1 even when it works.

## Step 4: Create the site files

```bash
cp -r sites/_template sites/<machine>
```

**`sites/<machine>/site.toml`:**

```toml
machine = "<machine>"
python = "python3"       # the full path from step 1, if you needed one
setup = ["module load <your MPI module>"]    # runs before installing; mpi4py builds against it
```

Leave `[publish]` and `[[run]]` as they are. If the machine has no MPI to build against, remove `mpi4py` from `extra_packages`. The MPI tests then skip themselves.

**`sites/<machine>/dragon_suite.sbatch`:** fill in `--account`, `--partition`, `--qos`, and `--gpus-per-node`, and add the same `module load` line. Compare with `sites/stellar-ai/dragon_suite.sbatch`, which shows the options that machine needed:

| Setting | When you need it |
|---|---|
| `--exclusive` | The partition shares nodes by default |
| `-t tcp` on the launcher line | The machine's high-speed network doesn't work with Dragon |
| `--network-prefix '<regex>'` | Dragon can't find the right network interfaces |

Keep `--total` below the job's `--time` limit, so results get written before SLURM stops the job. With `--time=00:35:00`, the template's `--total 1980` leaves 2 minutes.

## Step 5: Check the job script and preview a run

Find the latest release:

```bash
curl -s https://pypi.org/pypi/rhapsody-py/json |
    python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])"
```

These two commands use no allocation:

```bash
sbatch --test-only sites/<machine>/dragon_suite.sbatch
python3 hpc_monitor.py run --site sites/<machine>/site.toml --version <version> --dry-run
```

The first should print an estimated start time. A rejected account, partition, or QOS shows up here.

## Step 6: Run a baseline

This is required. The first time a machine sees a release, the monthly check only records it, because a run spends allocation. Without a baseline, the machine shows nothing until the next new release.

```bash
python3 hpc_monitor.py run --site sites/<machine>/site.toml --version <version>
```

This builds a fresh environment, clones the tests from the release tag, submits the job, and publishes the first status to the `results` branch.

Run a tick to follow the job until it ends, or wait for the schedule once step 8 is done:

```bash
squeue -u $USER
python3 hpc_monitor.py tick --site sites/<machine>/site.toml
tail ~/.hpc-monitor-grafana/<machine>/monitor.log
```

Check that these exist on the [`results` branch](https://github.com/radical-cybertools/radical-hpc-test-suite/tree/results/data):

- `data/<machine>.csv`
- `data/<machine>-tests.csv`
- `data/<machine>-usage.csv`

A red result here can be a real finding. Read the error before changing the setup.

## Step 7: Commit the site files

```bash
git add sites/<machine>
git commit -m "Add <Display Name> site configuration"
git push
```

This clone uses HTTPS, so the push needs your own GitHub login. The deploy key is only for publishing results. If this machine has no credentials set up, copy the two files to another checkout and commit them there. Take care not to commit `*.token` files or the deploy key.

## Step 8: Install the schedule

The tick must run about every 15 minutes, and each tick exits within seconds when there's nothing to do. Choose the first option the machine allows:

| Option | Use it when | Status |
|---|---|---|
| **SSH from a short job** | Compute nodes can't reach the internet, but can SSH to a login node | Running on Stellar AI |
| **Direct from a short job** | Compute nodes can reach PyPI and GitHub | Not tested yet |
| **`crontab` on a login node** | The site allows login-node cron | Not tested yet |

First, check for existing entries, because **`scrontab <file>` replaces the whole table**:

```bash
scrontab -l
```

If any are listed, merge them into your file first. Then edit `sites/<machine>/scrontab`:

- Fill in `<account>`, `<partition>`, `<short qos>`, and `<home>`.
- Replace `<repo>` with the full path to your clone, and `<machine>` with your name.
- Keep only one of the two schedule lines.
- Replace `python3` with the full path from step 1, if you needed one.

For SSH mode, test the connection from a compute node first:

```bash
srun --account=<account> --partition=<partition> --qos=<short qos> -N1 -t 2 \
    ssh -o BatchMode=yes <login-host> hostname
```

If that prints the host name, install the schedule and confirm:

```bash
scrontab sites/<machine>/scrontab
scrontab -l
```

After the next quarter hour, confirm a tick ran:

```bash
tail ~/.hpc-monitor-grafana/<machine>/monitor.log
tail ~/.hpc-monitor-grafana-scron.out
```

SLURM can show the tick job as `REQUEUED`. That's normal for `scrontab`, which reuses the job ID.

Sites that need multi-factor login often block SSH between nodes. If so, use direct mode or cron. Direct mode submits jobs from inside a SLURM job, which passes on that job's `SLURM_*` settings, so check the first real submission closely.

Commit the edited `scrontab` file too. It contains your username and paths, and the repo is public.

## Step 9: Add the machine to the dashboard

Do this after step 6, because Grafana needs the CSV files to exist. You need:

- A Grafana token with permission to edit dashboards, saved at `~/.hpc-monitor-grafana/grafana.token` with `chmod 600`.
- The Infinity data source UID, which is in `dashboard/hpc-monitor.json`.

List **every** connected machine with `--machine`, and the rest with `--planned`. The name before `=` is what the dashboard shows, and the file name must match the `machine` setting in `site.toml`. This example connects Delta:

```bash
BASE=https://raw.githubusercontent.com/radical-cybertools/radical-hpc-test-suite/results/data

python3 dashboard/build_dashboard.py \
    --datasource <infinity data source uid> \
    --machine "StellarAI=$BASE/stellar-ai.csv" \
    --machine "Delta=$BASE/delta.csv" \
    --planned NERSC --planned Bridges-2 \
    --out dashboard/hpc-monitor.json \
    --push https://<stack>.grafana.net
```

This updates the dashboard in place, so the public link stays the same. Then commit `dashboard/hpc-monitor.json`.

To try a change first, add `--uid hpc-monitor-design` to send it to the test dashboard.

## Step 10: Final check

Open the public dashboard in a private browser window, so you aren't logged in:

- [ ] The machine's card shows a pass rate, or a yellow status while the job is queued or running.
- [ ] The machine has its own section, and its run's arrow lists the tests.
- [ ] The run's `open` link shows its folder on GitHub, once the run has finished.
- [ ] "Last report" is recent. An idle machine updates it once a day.
- [ ] `scrontab -l` shows the tick, and `monitor.log` shows new lines every 15 minutes.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `sbatch --test-only` is rejected | Wrong account, partition, QOS, or GPU option | Compare with `sinfo` and `sacctmgr show assoc user=$USER` |
| Red: `Could not start the job` | `pip`, the tag checkout, or `sbatch` failed | Read `monitor.log`, fix it, then rerun step 6 |
| Every test fails during setup on CPU nodes | `v0.6.0` needs GPU nodes | Use a GPU partition |
| Red: `<file>.launcher` | A test file crashed its launcher or ran out of time | Read `~/.hpc-monitor-grafana/<machine>/runs/<run>/groups/<file>.log` |
| Dragon can't start or finds no network | The launcher needs network options | Try `-t tcp` and `--network-prefix` from step 4 |
| `publishing failed, will retry next tick` | Deploy key or GitHub access problem | Repeat the test in step 3 |
| "Last report" is days old | The schedule, SSH, or push stopped | Check `scrontab -l` and `monitor.log`, and run `tick` by hand |
| No run after a new release | The month's check had already happened | Wait for next month, or use `run --version` |
| The panel shows no data | The CSV doesn't exist yet, or GitHub's cache hasn't refreshed | Check the `results` branch, and wait about 5 minutes |

## Removing a machine

1. Remove its schedule line, or run `scrontab -r` if it was the only entry.
2. Delete its deploy key on GitHub.
3. Rebuild the dashboard without its `--machine` entry.

Its published results stay on the `results` branch.
