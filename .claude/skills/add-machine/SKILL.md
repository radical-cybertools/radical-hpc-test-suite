---
name: add-machine
description: Connect a new HPC machine to the monitor, with human approval before anything spends allocation or changes shared state.
disable-model-invocation: true
---

# Add a machine

Connect a machine by following `docs/adding-a-machine.md`, which has the commands. This skill adds who decides what, and when to stop. Read that guide and `docs/ARCHITECTURE.md` first.

**You need to be running on the target machine.** Run `hostname` and have the person confirm it's the right machine. If you aren't on it, give the person each step's commands to run there, and work from the output they paste back.

If this host already has a connected site, stop and ask. The monitor expects one site per machine.

## Collect these first

Ask for anything the person hasn't given you. You can suggest values from `sinfo` and `sacctmgr -nP show assoc user=$USER format=account,partition,qos`, but the person confirms them. Never guess.

- Machine name, in lowercase, for files
- Display name for the dashboard, and whether it replaces a grey "Not connected" card
- Account, GPU partition, QOS, and GPUs per node for the test job
- Modules to load, including MPI
- Launcher options and `--exclusive`, if a connected machine has similar hardware to copy from
- Which schedule option the site allows, and the partition and short QOS for the tick
- The login host, for the SSH schedule and for publishing when a job ends

## Steps and gates

Stop at each **STOP** and wait for a clear answer. Approval for one **STOP** doesn't cover the next. Rows without a **STOP** can go ahead without waiting.

| Guide step | You do | Person does |
|---|---|---|
| 1. Check the machine | Run the checks and report them | |
| 2. Clone | Use an existing clone if there is one, or clone. Run the offline tests | |
| 3. Deploy key | Check that no key exists, generate one, and print only the `.pub` line | **STOP:** adds it on GitHub with write access. Then you test it. |
| 4. Site files | Write the files and show the differences from the template | |
| 5. Checks | Run `sbatch --test-only` and the dry run | |
| 6. First run | Show the site files, account, nodes, time limit, queue estimate, and version. Say that it publishes public results | **STOP:** approves the files and the job, because it spends allocation and publishes |
| 6. Follow the job | The job should publish about 2 minutes after it ends. If it doesn't, run `tick --results-only`, which never starts a job. Report the result. The approval above covers this. If the queue is long, tell the person. You can continue with steps 7 and 8, or stop and resume later, because the job doesn't depend on this session | |
| 7. Commit | Print the commit steps | **STOP:** commits, or asks you to |
| 8. Test SSH | For the SSH schedule, show the short `srun` test | **STOP:** approves the test, because it's a real job |
| 8. Schedule | Show `scrontab -l`, the final merged file, and the temporary 5-minute test | **STOP:** approves installing and committing it. After one test tick works, install the real schedule |
| 9. Dashboard | Build on the test dashboard with `--uid hpc-monitor-design`, check it logged out, and show a screenshot | **STOP:** approves the public update |
| 10. Final check | Go through the checklist, and report each item as checked or not | |

## Rules for these steps

- **Existing deploy key:** if `~/.hpc-monitor-grafana/deploy_key` exists, stop and ask. It may be in use on a shared home directory. Never overwrite it. The guide's step 3 has the route for a second key.
- **Untested until the first run:** a passing dry run doesn't prove the key, modules, or publishing. Say so at the step 6 **STOP**.
- **Existing `scrontab` entries:** keep them. `scrontab <file>` replaces the whole table.
- **Secrets:** never print the private key or a Grafana token. Before any commit, check that neither is staged.
- **Grafana details:** the stack address and public links aren't in this repo, so ask the person for them. Without a token on this machine, skip building the dashboard, and hand the person the command for step 9.
- **Red test results:** a failed run counts as a working connection. Report the result, and don't change tests, budgets, or skips to turn it green.
- **Setup failures:** you can change this machine's site files, such as modules or launcher options. Tell the person what you changed and why.

## Stop and ask when

- The machine can't reach GitHub or PyPI as the guide requires.
- No schedule option is allowed.
- Site policy is unclear, such as whether login-node cron or outbound SSH is permitted.
- Multi-factor login is blocking you.
- A fix would break a hard constraint or invariant in `docs/ARCHITECTURE.md`, or need changes outside `sites/<machine>/`.

## Finish with a report

- What ran, and what was skipped
- Job IDs and results, including any failures
- What's been verified, and what hasn't
- What the person still needs to do
