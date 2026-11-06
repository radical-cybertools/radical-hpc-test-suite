# Specification: Centralized HPC HPE-TEST Monitoring and RHAPSODY Telemetry Integration

## Problem

We currently have monthly cron jobs running on four HPC machines. Each cron job checks whether a new software release is available and, when appropriate, submits an HPE-TEST suite as a SLURM job.

Today, monitoring requires manually logging into each HPC machine to determine whether tests passed or failed. This is inconvenient and does not provide a centralized view of test execution across machines and software releases.

We want to centralize test reporting and monitoring using a **free, hosted, third-party service**, without having to deploy or maintain our own backend infrastructure.

HPE-TEST execution will also be integrated with RHAPSODY, which already has OpenTelemetry (OTEL) instrumentation. Where it helps, we should leverage that instrumentation rather than introduce a parallel mechanism.

## Hard constraints

- **Zero cost.** No paid plans, now or as a requirement for basic use.
- **Viewers need no login.** The team must be able to view results without accounts. Results being fully public is acceptable.
- **No self-hosted infrastructure.** No servers, proxies, or databases for us to run.
- **No long-lived user processes on HPC.** Most of our machines kill user processes after about 7 days. Anything recurring must be short-lived invocations from cron or `scrontab`, not daemons or persistent agents.
- **No inbound connections to HPC machines.** Only outbound HTTPS/SSH from the HPC side.
- **Compute nodes have no internet access.** Only login nodes, or `scrontab` jobs where permitted, can reach external services.
- **Minimal maintenance and future-proofing matter more than features.** Prefer proven, widely used open-source tools over custom code. Prefer formats we own and can migrate (e.g. JUnit XML) over data locked in one vendor.

## Desired capabilities

- A centralized dashboard covering all four HPC machines.
- Programmatic reporting.
- Clear execution states: Green = passed, Yellow = pending/running, Red = failed.
- Visibility into failure reasons and relevant diagnostic output.
- Identification of HPC machine, software name, release/version, execution date, and SLURM job ID.
- Historical records across monthly runs and releases, with release-to-release comparison.
- Jobs killed by SLURM (timeout, out-of-memory, cancellation, node failure) must still show as failures, even though the job itself never reports.
- Extensible to additional RHAPSODY executions and HPC resources later.

## Existing infrastructure

- Four HPC machines, SLURM-based.
- Cron jobs for release detection and test submission. These can be reworked, e.g. to run daily instead of monthly, if that helps.
- RHAPSODY as the execution framework, with existing OTEL instrumentation.

Account for the existing setup rather than proposing a replacement instrumentation architecture. Keep RHAPSODY independent of whichever reporting backend is chosen.

## Prior investigation (to verify, not to take as given)

A preliminary assessment was done. Treat the points below as leads and starting evidence. Re-check current plan details and limits, challenge conclusions where warranted, and consider options not listed here.

### Options already looked at

**Hosted ClearML (Community plan)**
- Free hosted tier reportedly allows up to 3 users, 100 GB storage, 1M API calls/month.
- Task statuses map well to the states above, and logs are captured.
- Weak spot: viewer access. Sharing appears to be per-experiment via links, and recipients reportedly need a ClearML account. Reports are shareable only with ClearML logins or as PDF export. This likely conflicts with the no-login constraint.
- It is primarily an ML experiment tracker, so only a small part of it would be used.

**OTEL-native hosted observability (e.g. Grafana Cloud free tier)**
- Supports OTLP traces, metrics, and logs; externally shared (public) dashboards exist.
- Blocker found: the free tier reportedly keeps all signals for only 14 days, which breaks monthly history and release comparison.
- Data-model mismatch: spans export at completion, so a "running" state is awkward. Exit status is not the same as the HPE-TEST verdict; an explicit result attribute/event would be needed.
- Open question: what RHAPSODY's spans actually carry (machine name, SLURM job ID, software version, test verdict, a submit-time event).

**OTEL + ClearML combined**
- Looked like duplication with little gain at this scale (four machines, monthly). Worth revisiting only if cross-run performance correlation becomes a goal.

**Custom dashboard on GitHub Pages pulling from ClearML / backend proxy**
- Browser code can't hold API credentials, so this needs a proxy, i.e. hosted infrastructure. Probably ruled out by the constraints.

**CDash on my.cdash.org (Kitware)**
- Open-source test dashboard with long use in scientific/HPC software; Kitware offers free hosting.
- Models "machines (sites) × software-version (builds) × pass/fail over time" natively. Public projects are viewable without login.
- Submission via a CTest dashboard script (`ctest -S`) from the login node; no CMake project needed. CTest can also emit JUnit XML.
- Possible limits: a build cap (reported as 500 builds per project in 2019; verify current value), no true "running" state (closest is expected-but-missing builds), dependence on Kitware continuing free hosting, dated UI.
- Looked like the strongest fit so far, but not confirmed.

**Allure Report + GitHub Actions + GitHub Pages**
- Results pushed as JUnit XML to a public repo; stock Actions generate an Allure report with history and publish to Pages. Free and no-login for public repos.
- Data stays in our repo in a portable format.
- Downsides: more moving parts to maintain (Actions versions, Allure 2→3 transition, history handling); multi-machine view is less natural than CDash.

**More complete CI-style systems**
- *GitLab CI with Jacamar* at HPC centers that offer it: closest to an end-to-end solution, but depends on all four centers supporting it, and the center's GitLab typically requires login.
- *GitHub Actions self-hosted runners* on login nodes: conflicts with the no-long-lived-process rule at many centers. GitHub advises against self-hosted runners on public repos, and a private repo would require viewer logins.
- *HPC test frameworks* (ReFrame, Pavilion2, buildtest): test harnesses rather than hosted dashboards; they don't solve publishing.

### Execution-flow sketch discussed (one possibility, not a requirement)

A short-lived, `flock`-guarded script run daily from cron or `scrontab` on the login node:
1. Checks the release source; on a new version, `sbatch`es HPE-TEST and records the job ID in `$HOME`.
2. For recorded jobs, checks `sacct`; once a job is finished, reads its result file/log from the shared filesystem and submits to the chosen dashboard. This includes SLURM-killed jobs, reported as failures with the reason.
3. Optionally archives a portable result file (e.g. JUnit XML) to a git repo so history survives a vendor change.

Things noted along the way:
- A crontab lives on one specific login node, while `scrontab` survives login-node changes where available.
- Keep state in `$HOME`, not scratch.
- Each machine needs only a scoped submission token or deploy key, stored with `chmod 600`.

## Evaluation criteria

1. Feasibility within the hard constraints (free, no-login viewing, no infrastructure, no long-lived processes, outbound-only).
2. Maintenance burden over several years.
3. Future-proofing and data portability / vendor lock-in.
4. Amount of new code required.
5. Reuse of existing RHAPSODY OTEL instrumentation where it genuinely helps.
6. Credential and data security.
7. Fit with existing cron and SLURM workflows.
8. Representation of pending, running, passed, failed, and SLURM-killed states.
9. History and release comparison.
10. Diagnostic visibility (logs, failure reasons).
11. Extensibility to more machines and RHAPSODY executions.

## Expected outcome

- Verify the findings above against current documentation and plans, and correct anything outdated or wrong.
- Explore alternatives not listed above if they may fit the constraints better.
- Determine whether RHAPSODY's existing telemetry contributes anything to this use case, and what minimal additions (e.g. a result attribute or event) would be needed if so.
- Compare the viable options and recommend the simplest architecture that meets the hard constraints, stating explicitly any requirement that cannot be fully met (e.g. a true "running" state).
- Say whether the reporting logic warrants anything more than a small script/module. The preference is not to build a framework unless there's a clear reason.

Do not implement anything yet. First present the assessment and recommendation, then wait for confirmation before writing code.