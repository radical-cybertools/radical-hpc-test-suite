"""pytest plugin that records each test's result as it happens.

pytest writes JUnit only when it finishes, so a hung or killed run loses even the tests that
already failed, along with their errors. run_groups.py loads this with "-p pytest_live" and
reads the file named by HPC_MONITOR_LIVE_RESULTS when a run ends without JUnit.
"""

import json
import os

MESSAGE_LIMIT = 2000
DETAILS_LIMIT = 8000


def record(**fields):
    path = os.environ.get("HPC_MONITOR_LIVE_RESULTS")
    if path:
        with open(path, "a") as live:
            live.write(json.dumps(fields) + "\n")


def pytest_runtest_logstart(nodeid, location):
    record(test=nodeid, outcome=None, message="")


def pytest_runtest_logreport(report):
    # Setup and teardown count only if they went wrong, or if setup skipped the test.
    if report.when == "call" or report.outcome != "passed":
        crash = getattr(report.longrepr, "reprcrash", None)
        if crash:
            message = crash.message
        elif isinstance(report.longrepr, tuple):  # A skip: (file, line, reason)
            message = report.longrepr[-1]
        else:
            message = str(report.longrepr or "")
        details = str(report.longrepr)[-DETAILS_LIMIT:] if report.outcome == "failed" else ""
        record(test=report.nodeid, outcome=report.outcome, message=message[:MESSAGE_LIMIT],
               details=details)
