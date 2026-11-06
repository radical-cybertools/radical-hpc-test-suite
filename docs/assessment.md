# Assessment: central monitoring for HPC test suites

November 2026. This responds to the [spec](spec.md).

## Recommendation

Use **CDash on my.cdash.org** as the public dashboard, with **one small Python poller per machine**, and **archive JUnit XML to git**. The poller is a single stdlib-only module, and RHAPSODY needs no code changes.

CDash already models machines, builds, and test results over time, and is used by E3SM, Trilinos, Kokkos, WarpX, and buildtest. It meets every hard constraint in the spec. It is free hosting with no published guarantee, so the archive keeps the data ours and gives us a way out.

**Tested on the hosted service:** the yellow placeholder and release comparison both work, with limits described below. See [spike results](../spike/RESULTS.md).

## Requirements that can't be fully met

- **Yellow state.** CDash has no running or pending state, and its colours are fixed. We show pending as a row with blank test cells and running as an orange "Not Run" cell. Updates arrive about every 15 minutes, and the cell stays orange if the scheduler dies.
- **CDash internals.** Replacing a row is tested by CDash's own test suite, but I found no documentation promising it. The monitor reads back each update, so a change in behaviour would be detected.
- **Version display.** The dashboard row cuts the version to six characters, so `0.10.12` would show as `0.10.1`. The full version is available on the test page.
- **Machines that stop reporting.** A dead machine can't report itself. CDash's expected-build feature should show this, but is untested.
- **Hosting guarantee.** Kitware publishes no limits or guarantee for the free service.
- **Viewing the archive.** While the GitHub repo is private, the archive needs a login. The dashboard does not.

## How the prior investigation held up

| Claim | Finding | Basis |
|---|---|---|
| CDash public projects need no login | Confirmed | Anonymous API reads |
| CDash cap of 500 builds | Probably still about 500, and now it silently deletes the oldest builds | Source, plus 23 of 97 non-empty projects at 480–520 builds; the value is inferred |
| Not in the spec: CDash retention | New risk: the default is 90 days and must be raised for monthly history. Live projects go up to 800 days. | Source, live query |
| CDash suits long history | Unproven beyond 2 years. The oldest build found on the server is from 2024-11-18. | Live query |
| CDash has no running state | True, but can be approximated (see above) | Tested on the hosted service |
| CDash may be fading | Wrong: v5.5.2 appears to be from Oct 2026 and the source had a commit on the day checked | Releases, repo |
| Grafana free tier keeps 14 days | Confirmed. External dashboards also don't support Tempo or variables. | Grafana docs |
| ClearML free limits | Confirmed. Docs imply viewers need an account but don't state it. Dashboards are Pro-only. | ClearML docs |
| Allure handles multiple machines poorly | Partly outdated: Allure 3 has per-platform environments and JSONL history, but the official Pages example doesn't carry history over | Allure docs |

Not re-verified, because they looked ruled out on architecture alone: GitLab/Jacamar, self-hosted runners, and ReFrame-style frameworks. Searches found no free, no-login hosted alternative, but returned nothing on Codecov, ReportPortal, Datadog, or Buildkite, so those remain unchecked.

## Options compared

| | CDash + git archive | Allure + Actions + Pages | Own static page + git |
|---|---|---|---|
| Meets the hard constraints | Yes | Yes | Yes |
| New code | Poller only | Poller, workflow, history handling | Poller and the dashboard itself |
| Maintenance | Low; depends on Kitware | Medium; Actions and Allure churn | Ours indefinitely |
| Machine × version view | Built in | Via environments | We build it |
| Yellow state | Approximate (orange) | Would need a workaround | True yellow |
| Data ownership | Through the archive | Native | Native |

**Fallback:** if Kitware's hosting goes away, the archive can feed Allure with one new workflow and no changes on the HPC side.

## Does RHAPSODY's telemetry help?

It is useful as a diagnostic source, but it can't drive the status.

- **What it provides:** per-task status, duration, and exception class name. With `checkpoint_interval` set, the JSONL checkpoint should show what was running when SLURM killed a job.
- **What it lacks:** machine name, SLURM job ID, software version, test verdict, any event at `sbatch` time, and error text. It also can't report live: exporters are off by default and compute nodes have no internet.
- **Additions needed:** none. The verdict comes from JUnit, and the other fields from `sacct` and the poller's state. The HPC suite's fixtures don't start telemetry today, so using checkpoints is deferred.

## Does this need more than a small module?

No. One module of about 300 lines plus per-machine directories is enough. Each machine owns its sbatch script, so machine differences never reach the code.

## Capacity

Each package uses 48 builds a year across four machines, comfortably inside the inferred cap. Daily RHAPSODY runs would add about 1,460 a year and push out older history within months, so those should get their own CDash project.

## Scheduling

`crontab` is denied for our account on Stellar AI and its compute nodes have no internet, so a `scrontab` job there will SSH to the login node, which works with no key setup. `spike/probe_network.sbatch` checks each machine for the same options.

## Sources

Live checks ran against my.cdash.org on 2026-11-05 using anonymous, read-only queries.

- **CDash:** [hosting](https://www.cdash.org/hosting/), [source](https://github.com/Kitware/CDash), [releases](https://github.com/Kitware/CDash/releases), [GraphQL docs](https://github.com/Kitware/CDash/blob/master/docs/graphql.md), [issue #921](https://github.com/Kitware/CDash/issues/921)
- **Grafana:** [pricing](https://grafana.com/pricing/), [shared dashboards](https://grafana.com/docs/grafana/latest/dashboards/share-dashboards-panels/shared-dashboards/)
- **ClearML:** [pricing](https://clear.ml/pricing), [task sharing](https://clear.ml/docs/latest/docs/webapp/webapp_exp_sharing/), [reports](https://clear.ml/docs/latest/docs/webapp/webapp_reports/)
- **Allure:** [GitHub Pages guide](https://allurereport.org/docs/guides/github-pages/), [history](https://allurereport.org/docs/history-and-retries/), [Allure 3 releases](https://github.com/allure-framework/allure3/releases)
- **GitHub:** [Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits), [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [workflow disabling](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)
- **Other JUnit options:** [Automated Future](https://dev.to/stevepryde/a-better-home-for-your-automated-test-results-3951), [Depot](https://depot.dev/changelog/2026-06-02-test-results-beta), [dorny/test-reporter](https://github.com/dorny/test-reporter), [publish-unit-test-result-action](https://github.com/EnricoMi/publish-unit-test-result-action)
