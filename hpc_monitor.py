#!/usr/bin/env python3
"""Report SLURM test-suite runs to a public CDash dashboard.

Short-lived and safe to repeat: each invocation advances open runs and exits, so
no process stays behind on the machine. See README.md and sites/README.md.

    hpc_monitor.py tick --site sites/<machine>/site.toml [--dry-run]
    hpc_monitor.py run  --site sites/<machine>/site.toml --version 0.6.0 [--dry-run]

The dashboard sequence, and why it looks like this, is in spike/RESULTS.md:

    pending   Configure.xml + Update.xml             row with version, blank test cells
    running   Test.xml (notrun), Done.xml            orange Not Run cell
    final     Update.xml, Test.xml, Done.xml         green or red, earlier entries replaced

CDash processes uploads through a queue, so every update is read back before the
next step, and a row that stays wrong is repaired on a later tick.
"""

import argparse
import calendar
import datetime
import fcntl
import getpass
import hashlib
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from xml.sax.saxutils import quoteattr

log = logging.getLogger("hpc_monitor")

DEFAULT_TIMING = {
    "readback_seconds": 180,  # how long one tick waits for an upload to appear
    "replace_gap_seconds": 600,  # minimum time after Done.xml before replacing a row
    "repair_after_seconds": 1800,  # how long an upload gets to apply before repair
    "max_repairs": 3,
    "missing_grace_seconds": 3600,  # accounting can lag behind a fresh submission
    "release_check_seconds": 86400,
}
DEFAULT_SOURCE_REPO = "https://github.com/radical-cybertools/rhapsody.git"
PYPI_URL = "https://pypi.org/pypi/{package}/json"

TERMINAL_STATES = {
    "BOOT_FAIL", "CANCELLED", "COMPLETED", "DEADLINE", "FAILED",
    "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "TIMEOUT",
}
WAITING_STATES = {"PENDING", "REQUEUED", "REQUEUE_HOLD", "RESV_DEL_HOLD", "SUSPENDED", "STOPPED"}
RUNNING_STATES = {"RUNNING", "COMPLETING", "CONFIGURING", "RESIZING", "SIGNALING", "STAGE_OUT"}
SACCT_FIELDS = ("JobID", "State", "ExitCode", "Elapsed", "Start", "End", "NodeList", "Reason")

MAX_OUTPUT_BYTES = 64 * 1024
LOG_TAIL_LINES = 200
REPLACED = (-1, -1, -1)  # counts CDash reports for a row that has no tests
RUNNING_COUNTS = (0, 0, 1)
STATUS_TEST = "slurm-job"
STOP, NEXT, CHANGED = "stop", "next", "changed"


class TransientError(Exception):
    """Something outside our control failed; the next tick retries from the same stage."""


def now():
    return time.time()


# --------------------------------------------------------------- public-safe text


def scrub(text, user=None, home=None):
    """Remove personal paths, secret-looking values, and characters XML cannot hold."""
    home = home if home is not None else os.path.expanduser("~")
    user = user if user is not None else getpass.getuser()
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    text = re.sub(r"[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]", "", text)
    text = re.sub(
        r"(?i)\b([A-Z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_.-]*)(\s*[=:]\s*)\S+",
        r"\1\2<redacted>", text,
    )
    text = re.sub(r"(?i)\b(bearer\s+)\S+", r"\1<redacted>", text)
    if home and home != "/":
        text = text.replace(home, "~")
    if user:
        text = re.sub(rf"\b{re.escape(user)}\b", "<user>", text)
    return text


def trim(text, limit=MAX_OUTPUT_BYTES):
    """Keep the end of long output, where the failure usually is."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    return "[output trimmed]\n" + data[-limit:].decode("utf-8", "ignore")


def read_tail(path, lines=LOG_TAIL_LINES):
    try:
        text = pathlib.Path(path).read_text(errors="replace")
    except OSError:
        return "(no job log found)"
    return "\n".join(text.splitlines()[-lines:])


# ----------------------------------------------------------------- JUnit and verdict


def parse_junit(path):
    """Return (tests, skipped) from pytest JUnit, raising ValueError if unusable."""
    try:
        root = ET.parse(path).getroot()
    except OSError as err:
        raise ValueError("JUnit file is missing") from err
    except ET.ParseError as err:
        raise ValueError(f"JUnit file is corrupt: {err}") from err
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"JUnit file has unexpected root <{root.tag}>")

    tests, skipped = [], []
    for case in root.iter("testcase"):
        name = ".".join(part for part in (case.get("classname"), case.get("name")) if part)
        if case.find("skipped") is not None:
            skipped.append(name)
            continue
        problems = [e for e in case if e.tag in ("failure", "error")]
        output = "\n".join(
            part for e in problems for part in (e.get("message"), e.text) if part
        )
        try:
            seconds = float(case.get("time", 0))
        except ValueError:
            seconds = 0.0
        tests.append({
            "name": name,
            "status": "failed" if problems else "passed",
            "output": output,
            "seconds": seconds,
        })
    return tests, skipped


def decide(job, junit_path):
    """Turn the SLURM result and JUnit file into a verdict. Pure apart from reading the file."""
    state = (job or {}).get("state", "UNKNOWN")
    try:
        tests, skipped = parse_junit(junit_path)
        junit_error = None
    except ValueError as err:
        tests, skipped, junit_error = [], [], str(err)

    failed = sum(t["status"] == "failed" for t in tests)
    executed = len(tests)
    job_ok = False

    if state == "UNKNOWN":
        ok, reason = False, "job is no longer known to SLURM accounting"
    elif state == "COMPLETED" and junit_error:
        ok, reason = False, f"job completed but no results: {junit_error}"
    elif state == "COMPLETED" and not executed:
        ok, reason = False, "job completed but no tests executed"
    elif state == "COMPLETED" and failed:
        ok, reason, job_ok = False, f"{failed} of {executed} tests failed", True
    elif state == "COMPLETED":
        ok, reason, job_ok = True, f"{executed} tests passed", True
    elif state == "FAILED" and failed:
        # pytest exits non-zero when a test fails, so this is a test result, not a crash.
        ok, reason, job_ok = False, f"{failed} of {executed} tests failed", True
    else:
        detail = (job or {}).get("reason") or ""
        detail = "" if detail in ("None", "") else f" ({detail})"
        ok, reason = False, f"SLURM ended the job: {state}{detail}"
        if junit_error:
            reason += f"; {junit_error}"

    return {
        "ok": ok, "reason": reason, "job_ok": job_ok,
        "tests": tests, "skipped": skipped,
    }


# ------------------------------------------------------------------ CDash documents


def stamp_for(timestamp, track):
    return time.strftime("%Y%m%d-%H%M", time.gmtime(timestamp)) + f"-{track}"


def _datetime(timestamp):
    return time.strftime("%b %d %H:%M UTC", time.gmtime(timestamp))


def _site_open(site, build, stamp):
    return (f"<Site BuildName={quoteattr(build)} BuildStamp={quoteattr(stamp)} "
            f"Name={quoteattr(site)} Generator=\"hpc-monitor\">")


def configure_xml(site, build, stamp, start, command, message):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{_site_open(site, build, stamp)}
<Configure><StartDateTime>{_datetime(start)}</StartDateTime><StartConfigureTime>{int(start)}</StartConfigureTime>
<ConfigureCommand>{escape(command)}</ConfigureCommand><Log>{escape(message)}</Log>
<ConfigureStatus>0</ConfigureStatus><EndDateTime>{_datetime(start)}</EndDateTime>
<EndConfigureTime>{int(start)}</EndConfigureTime><ElapsedMinutes>0</ElapsedMinutes></Configure></Site>"""


def update_xml(site, build, stamp, start, version):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Update mode="Client" Generator="hpc-monitor"><Site>{escape(site)}</Site><BuildName>{escape(build)}</BuildName>
<BuildStamp>{escape(stamp)}</BuildStamp><StartDateTime>{_datetime(start)}</StartDateTime><StartTime>{int(start)}</StartTime>
<UpdateCommand>none</UpdateCommand><UpdateType>GIT</UpdateType><EndDateTime>{_datetime(start)}</EndDateTime>
<EndTime>{int(start)}</EndTime><ElapsedMinutes>0</ElapsedMinutes><UpdateReturnStatus></UpdateReturnStatus>
<Revision>{escape(version)}</Revision><PriorRevision>{escape(version)}</PriorRevision></Update>"""


def test_xml(site, build, stamp, start, end, tests, job_id):
    items = "".join(
        f"""<Test Status="{t['status']}"><Name>{escape(t['name'])}</Name><Path>./{escape(build)}</Path>
<FullName>./{escape(build)}/{escape(t['name'])}</FullName><FullCommandLine>pytest</FullCommandLine>
<Results><NamedMeasurement type="numeric/double" name="Execution Time"><Value>{t.get('seconds', 0):.3f}</Value></NamedMeasurement>
<NamedMeasurement type="text/string" name="SLURM Job ID"><Value>{escape(str(job_id))}</Value></NamedMeasurement>
<Measurement><Value>{escape(t['output'])}</Value></Measurement></Results></Test>"""
        for t in tests
    )
    minutes = max(0, int((end - start) / 60))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{_site_open(site, build, stamp)}
<Testing><StartDateTime>{_datetime(start)}</StartDateTime><StartTestTime>{int(start)}</StartTestTime>
<TestList/>{items}<EndDateTime>{_datetime(end)}</EndDateTime><EndTestTime>{int(end)}</EndTestTime>
<ElapsedMinutes>{minutes}</ElapsedMinutes></Testing></Site>"""


def done_xml(end):
    return f"<Done><buildId>0</buildId><time>{int(end)}</time></Done>"


class CDash:
    """Uploads through submit.php, as ctest_submit does, and reads through GraphQL."""

    def __init__(self, url, project, token_file, dry_run=False):
        self.url = url.rstrip("/")
        self.project = project
        self.token_file = pathlib.Path(token_file)
        self.dry_run = dry_run

    def put(self, filename, body, site, build, stamp):
        if self.dry_run:
            log.info("[dry run] would upload %s for %s %s %s", filename, site, build, stamp)
            return
        data = body.encode("utf-8")
        query = urllib.parse.urlencode({
            "project": self.project, "FileName": filename, "build": build, "site": site,
            "stamp": stamp, "MD5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        })
        request = urllib.request.Request(f"{self.url}/submit.php?{query}", data=data, method="PUT")
        if self.token_file.exists():
            token = self.token_file.read_text().strip()
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                text = response.read().decode("utf-8", "replace")
        except OSError as err:
            raise TransientError(f"upload of {filename} failed: {err}") from err
        if "<status>OK</status>" not in text:
            raise TransientError(f"CDash rejected {filename}: {text[:300]}")
        log.info("uploaded %s for %s %s %s", filename, site, build, stamp)

    def row(self, site, build, stamp):
        """Return (pass, fail, notrun) if exactly one row matches, otherwise None."""
        query = (
            '{ projects(filters:{eq:{name:%s}}) { edges { node { '
            'builds(first:100, filters:{eq:{name:%s}}) { edges { node { '
            'stamp site { name } passedTestsCount failedTestsCount notRunTestsCount '
            '} } } } } } }'
        ) % (json.dumps(self.project), json.dumps(build))
        request = urllib.request.Request(
            f"{self.url}/graphql", data=json.dumps({"query": query}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except (OSError, ValueError) as err:
            raise TransientError(f"reading CDash failed: {err}") from err
        if "errors" in result:
            raise TransientError(f"CDash query failed: {result['errors'][:1]}")
        edges = result["data"]["projects"]["edges"]
        if not edges:
            raise TransientError(f"CDash project {self.project} is not visible")
        rows = [
            e["node"] for e in edges[0]["node"]["builds"]["edges"]
            if e["node"]["stamp"] == stamp and e["node"]["site"]["name"] == site
        ]
        if len(rows) != 1:
            return None
        node = rows[0]
        return tuple(
            -1 if node[key] is None else node[key]
            for key in ("passedTestsCount", "failedTestsCount", "notRunTestsCount")
        )


# --------------------------------------------------------------------------- config


def load_config(path):
    path = pathlib.Path(path).resolve()
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    for key in ("machine", "venv", "run", "cdash"):
        if key not in config:
            raise SystemExit(f"{path}: missing required setting {key!r}")
    config["site_dir"] = path.parent
    config["timing"] = DEFAULT_TIMING | config.get("timing", {})
    state_root = os.environ.get("HPC_MONITOR_HOME") or config.get("state_dir") or "~/.hpc-monitor"
    config["state_root"] = pathlib.Path(state_root).expanduser()
    config["state_dir"] = config["state_root"] / config["machine"]
    config["cdash"].setdefault("url", "https://my.cdash.org")
    config["cdash"].setdefault("track", "Experimental")
    config["cdash"].setdefault("token_file", str(config["state_root"] / "cdash.token"))
    for run in config["run"]:
        for key in ("name", "package", "job_script"):
            if key not in run:
                raise SystemExit(f"{path}: a [[run]] entry is missing {key!r}")
        run.setdefault("install", f"{run['package']}[dragon]=={{version}}")
        run.setdefault("extra_packages", ["pytest", "pytest-asyncio"])
        run.setdefault("record_versions", ["dragonhpc"])
        run.setdefault("require_versions", {})
        run.setdefault("source_repo", DEFAULT_SOURCE_REPO)
        run.setdefault("source_tag", "v{version}")
        run.setdefault("pypi_url", PYPI_URL.format(package=run["package"]))
    return config


# -------------------------------------------------------------------------- monitor


class Monitor:
    def __init__(self, config, dry_run=False):
        self.config = config
        self.dry_run = dry_run
        self.site = config["machine"]
        self.timing = config["timing"]
        self.state_file = config["state_dir"] / "state.json"
        self.state = {"runs": [], "seen": {}, "last_release_check": 0}
        if self.state_file.exists():
            self.state |= json.loads(self.state_file.read_text())
        cdash = config["cdash"]
        self.cdash = CDash(cdash["url"], cdash["project"], cdash["token_file"], dry_run)
        self.deadline = 0

    # ----- plumbing

    def save(self):
        if self.dry_run:
            return
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=1))
        os.replace(temporary, self.state_file)

    def shell(self, command, check=True, env=None, cwd=None):
        """Run a command that only reads, and return its stdout."""
        result = subprocess.run(
            command, capture_output=True, text=True, env=env, cwd=cwd, check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[-500:] or
                               f"{command[0]} exited with {result.returncode}")
        return result.stdout

    def spec(self, run):
        for spec in self.config["run"]:
            if spec["name"] == run["name"]:
                return spec
        raise RuntimeError(f"run {run['name']!r} is no longer in site.toml")

    def open_runs(self):
        return [r for r in self.state["runs"] if r["stage"] != "archived"]

    def post(self, run, filename, body):
        self.cdash.put(filename, body, self.site, run["name"], run["stamp"])
        run["posted_at"] = now()

    # ----- entry points

    def tick(self):
        failures = 0
        log.info("tick: %d open run(s)", len(self.open_runs()))
        for run in self.open_runs():
            failures += self.advance(run)
        if not self.open_runs():
            self.check_releases()
        return 1 if failures else 0

    def manual_run(self, name, version):
        if self.open_runs():
            run = self.open_runs()[0]
            log.error("run %s %s is still open; one run at a time shares the environment",
                      run["name"], run["version"])
            return 1
        specs = [s for s in self.config["run"] if name in (None, s["name"])]
        if not specs:
            log.error("no [[run]] named %s", name)
            return 1
        run = self.start_run(specs[0], version)
        return self.advance(run) if run else 0

    def check_releases(self):
        if now() - self.state["last_release_check"] < self.timing["release_check_seconds"]:
            return
        started = False
        for spec in self.config["run"]:
            try:
                with urllib.request.urlopen(spec["pypi_url"], timeout=60) as response:
                    latest = json.load(response)["info"]["version"]
            except (OSError, ValueError, KeyError) as err:
                log.warning("could not read latest %s version: %s", spec["package"], err)
                return
            seen = self.state["seen"].get(spec["name"])
            if seen is None:
                # First sight is recorded only: a baseline run spends allocation, so it's manual.
                log.info("recording %s %s; use the run command for a baseline", spec["name"], latest)
                self.state["seen"][spec["name"]] = latest
            elif seen != latest:
                log.info("new release: %s %s (was %s)", spec["package"], latest, seen)
                run = self.start_run(spec, latest)
                if run:
                    self.advance(run)
                started = True
                break
        if not started:
            # A started run keeps this open, so a second [[run]] entry follows once it closes.
            self.state["last_release_check"] = now()
        self.save()

    # ----- starting a run

    def start_run(self, spec, version):
        created = now()
        stamp = stamp_for(created, self.config["cdash"]["track"])
        job_name = "hpcmon-" + re.sub(r"\W+", "-", f"{spec['name']}-{stamp}")
        run_dir = self.config["state_dir"] / "runs" / job_name
        run = {
            "name": spec["name"], "version": version, "stamp": stamp, "job_name": job_name,
            "stage": "submitting", "created_at": created, "run_dir": str(run_dir),
            "source_dir": str(run_dir / "source"), "job_id": None, "repairs": 0,
            "versions": {},
        }
        if self.dry_run:
            self.show_plan(spec, run)
            return None
        run_dir.mkdir(parents=True, exist_ok=True)
        self.state["seen"][spec["name"]] = version
        self.state["runs"].append(run)
        self.save()
        return run

    def show_plan(self, spec, run):
        version = run["version"]
        python = pathlib.Path(self.config["venv"]) / "bin" / "python"
        installed = self.installed(python, spec["package"])
        wanted = self.packages_to_install(python, spec, version, run["source_dir"])
        log.info("[dry run] %s installed: %s, wanted: %s", spec["package"], installed, version)
        if wanted:
            log.info("[dry run] would run: pip install --upgrade %s", " ".join(wanted))
        log.info("[dry run] would clone %s at %s", spec["source_repo"],
                 spec["source_tag"].format(version=version))
        log.info("[dry run] would submit %s as %s",
                 self.config["site_dir"] / spec["job_script"], run["job_name"])
        log.info("[dry run] would post pending row %s %s %s", self.site, run["name"], run["stamp"])

    def installed(self, python, package):
        code = "import importlib.metadata as m, sys; print(m.version(sys.argv[1]))"
        try:
            return self.shell([str(python), "-c", code, package]).strip() or None
        except (RuntimeError, OSError):
            return None

    def packages_to_install(self, python, spec, version, source_dir):
        """Only what's needed, so a matching environment isn't touched."""
        wanted = [p for p in spec["extra_packages"] if self.installed(python, p) is None]
        # A source build can change without its version changing, so it's always installed.
        from_source = "{source_dir}" in spec["install"]
        if from_source or self.installed(python, spec["package"]) != version:
            wanted.insert(0, spec["install"].format(version=version, source_dir=source_dir))
        return wanted

    def prepare_environment(self, spec, run):
        """Bring the shared venv to this version. Only called with no other run open."""
        python = pathlib.Path(self.config["venv"]) / "bin" / "python"
        version = run["version"]
        source = pathlib.Path(run["source_dir"])
        if source.exists():
            shutil.rmtree(source)
        tag = spec["source_tag"].format(version=version)
        self.shell(["git", "clone", "--quiet", "--depth", "1", "--branch", tag,
                    spec["source_repo"], str(source)])
        wanted = self.packages_to_install(python, spec, version, run["source_dir"])
        if wanted:
            setup = " && ".join(self.config.get("setup", []))
            command = [str(python), "-m", "pip", "install", "--upgrade", *wanted]
            if setup:
                quoted = " ".join(f"'{part}'" for part in command)
                command = ["bash", "-c", f"{setup} && {quoted}"]
            log.info("installing %s", " ".join(wanted))
            self.shell(command)
        actual = self.installed(python, spec["package"])
        if actual != version:
            raise RuntimeError(f"environment has {spec['package']} {actual}, wanted {version}")
        for package, required in spec["require_versions"].items():
            actual = self.installed(python, package)
            if actual != required:
                raise RuntimeError(f"environment has {package} {actual}, required {required}")
        run["versions"] = {
            p: self.installed(python, p) or "unknown" for p in spec["record_versions"]
        }
        commit = self.shell(["git", "-C", str(source), "rev-parse", "--short=12", "HEAD"]).strip()
        run["versions"]["source commit"] = commit

    def submit_job(self, spec, run):
        script = (self.config["site_dir"] / spec["job_script"]).resolve()
        if not script.exists():
            raise RuntimeError(f"job script not found: {script}")
        env = os.environ | {
            "HPC_MONITOR_RUN_DIR": run["run_dir"],
            "HPC_MONITOR_VENV": str(self.config["venv"]),
            "HPC_MONITOR_SOURCE_DIR": run["source_dir"],
            "HPC_MONITOR_VERSION": run["version"],
            "HPC_MONITOR_REPO_DIR": str(pathlib.Path(__file__).resolve().parent),
        }
        output = self.shell(
            ["sbatch", "--parsable", f"--job-name={run['job_name']}",
             f"--output={run['run_dir']}/slurm.out", "--export=ALL", str(script)],
            env=env, cwd=run["run_dir"],
        )
        return output.strip().split(";")[0]

    def find_submitted_job(self, run):
        """Recover the job ID if a previous tick died right after sbatch."""
        try:
            output = self.shell(["sacct", "-X", "--noheader", "--parsable2",
                                 f"--name={run['job_name']}", "--format=JobID"])
        except (RuntimeError, OSError) as err:
            raise TransientError(f"sacct failed: {err}") from err
        lines = output.split()
        return lines[0] if lines else None

    # ----- job state

    def job_state(self, run):
        """Return a job dict with a 'kind' of waiting, running, or finished."""
        if run.get("job"):
            return run["job"]
        fields = ",".join(SACCT_FIELDS)
        try:
            output = self.shell(["sacct", "-j", str(run["job_id"]), "-X", "--noheader",
                                 "--parsable2", f"--format={fields}"])
        except (RuntimeError, OSError) as err:
            raise TransientError(f"sacct failed: {err}") from err
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            if now() - run["created_at"] < self.timing["missing_grace_seconds"]:
                return {"kind": "waiting", "state": "PENDING"}
            log.warning("job %s is missing from sacct", run["job_id"])
            return {"kind": "finished", "state": "UNKNOWN"}
        values = dict(zip(SACCT_FIELDS, lines[0].split("|"), strict=False))
        raw_state = values.get("State", "")
        state = raw_state.split()[0].rstrip("+") if raw_state.split() else ""
        job = {
            "state": state, "raw_state": raw_state, "exit_code": values.get("ExitCode", ""),
            "elapsed": values.get("Elapsed", ""), "start": values.get("Start", ""),
            "end": values.get("End", ""), "nodes": values.get("NodeList", ""),
            "reason": values.get("Reason", ""),
        }
        if state in TERMINAL_STATES:
            job["kind"] = "finished"
        elif state in WAITING_STATES:
            job["kind"] = "waiting"
        else:
            if state not in RUNNING_STATES:
                log.warning("treating unrecognised job state %r as running", state)
            job["kind"] = "running"
        return job

    def job_times(self, run):
        job = run.get("job") or {}

        def parse(value, fallback):
            try:
                return datetime.datetime.fromisoformat(value).timestamp()
            except (TypeError, ValueError):
                return fallback

        start = parse(job.get("start"), run["created_at"])
        return start, max(start, parse(job.get("end"), now()))

    # ----- final result

    def build_final(self, run):
        """Freeze the verdict once, so every retry uploads the same result."""
        spec_job_ok = run.get("setup_error")
        run_dir = pathlib.Path(run["run_dir"])
        if spec_job_ok:
            verdict = {"ok": False, "job_ok": False, "tests": [], "skipped": [],
                       "reason": f"could not start the job: {run['setup_error']}"}
        else:
            verdict = decide(run.get("job"), run_dir / "junit.xml")
        job = run.get("job") or {}
        lines = [
            f"Verdict: {'PASSED' if verdict['ok'] else 'FAILED'}: {verdict['reason']}",
            f"Machine: {self.site}",
            f"SLURM job ID: {run.get('job_id')}",
            f"Version: {run['version']}",
            *[f"{package}: {value}" for package, value in run["versions"].items()],
            f"SLURM state: {job.get('raw_state') or job.get('state') or 'not submitted'}",
            f"Exit code: {job.get('exit_code', '')}",
            f"Elapsed: {job.get('elapsed', '')}",
            f"Nodes: {job.get('nodes', '')}",
        ]
        if verdict["skipped"]:
            lines += [f"Skipped tests ({len(verdict['skipped'])}):",
                      *[f"  {name}" for name in verdict["skipped"]]]
        lines += ["", "Job log (end):", read_tail(run_dir / "slurm.out")]
        status = {
            "name": STATUS_TEST, "status": "passed" if verdict["job_ok"] else "failed",
            "output": "\n".join(lines), "seconds": 0.0,
        }
        tests = [*verdict["tests"], status]
        for test in tests:
            test["output"] = trim(scrub(test["output"]))
        passed = sum(t["status"] == "passed" for t in tests)
        failed = len(tests) - passed
        run["verdict"] = {"ok": verdict["ok"], "reason": verdict["reason"],
                          "skipped": verdict["skipped"], "passed": passed, "failed": failed}
        run["expected"] = [passed, failed, 0]
        (run_dir / "final_tests.json").write_text(json.dumps(tests))
        log.info("%s %s: %s", run["name"], run["version"], status["output"].split("\n", 1)[0])

    def final_test_xml(self, run):
        tests = json.loads((pathlib.Path(run["run_dir"]) / "final_tests.json").read_text())
        start, end = self.job_times(run)
        return test_xml(self.site, run["name"], run["stamp"], start, end, tests, run.get("job_id"))

    def update_body(self, run):
        return update_xml(self.site, run["name"], run["stamp"], run["created_at"], run["version"])

    def done_body(self, run):
        return done_xml(self.job_times(run)[1])

    def finish_job(self, run, job):
        run["job"] = job
        if "expected" not in run:
            self.build_final(run)

    # ----- stages: each returns STOP, NEXT, or CHANGED

    def advance(self, run):
        """Move a run on, changing what the dashboard shows at most once. Returns 1 on failure."""
        self.deadline = now() + self.timing["readback_seconds"]
        changed = False
        try:
            while run["stage"] != "archived":
                result = getattr(self, f"stage_{run['stage']}")(run, changed)
                self.save()
                if result == STOP:
                    break
                changed = changed or result == CHANGED
        except (TransientError, RuntimeError) as err:
            log.warning("%s %s, will retry next tick: %s", run["name"], run["stage"], err)
            self.save()
            return 1
        return 0

    def stage_submitting(self, run, _changed):
        spec = self.spec(run)
        if self.dry_run:
            log.info("[dry run] run %s is waiting to be submitted", run["job_name"])
            return STOP
        job_id = self.find_submitted_job(run)
        if not job_id:
            try:
                self.prepare_environment(spec, run)
                job_id = self.submit_job(spec, run)
            except (RuntimeError, OSError) as err:
                log.error("could not start %s %s: %s", run["name"], run["version"], err)
                run["setup_error"] = str(err)
        run["job_id"] = job_id
        run["stage"] = "submitted"
        return NEXT

    def stage_submitted(self, run, _changed):
        if run.get("setup_error"):
            if "expected" not in run:
                self.build_final(run)
            return self.post_final_fresh(run)
        command = f"sbatch {self.spec(run)['job_script']}"
        message = f"Submitted SLURM job {run['job_id']} for version {run['version']}"
        self.post(run, "Configure.xml", configure_xml(
            self.site, run["name"], run["stamp"], run["created_at"], command, message))
        self.post(run, "Update.xml", self.update_body(run))
        run["stage"] = "pending_posted"
        return CHANGED

    def post_final_fresh(self, run):
        """Upload to a row with nothing to replace."""
        self.post(run, "Update.xml", self.update_body(run))
        self.post(run, "Test.xml", self.final_test_xml(run))
        run["stage"] = "final_posted"
        return CHANGED

    def stage_pending_posted(self, run, changed):
        if changed:
            return STOP
        job = self.job_state(run)
        if job["kind"] == "waiting":
            return STOP
        if job["kind"] == "running":
            self.post_running(run)
            return CHANGED
        # Finished between ticks: nothing to replace, so the result is simply added.
        self.finish_job(run, job)
        self.post(run, "Test.xml", self.final_test_xml(run))
        run["stage"] = "final_posted"
        return CHANGED

    def post_running(self, run):
        tests = [{"name": STATUS_TEST, "status": "notrun", "seconds": 0.0,
                  "output": f"RUNNING: SLURM job {run['job_id']}, version {run['version']}"}]
        start, _ = self.job_times(run)
        self.post(run, "Test.xml", test_xml(
            self.site, run["name"], run["stamp"], start, now(), tests, run["job_id"]))
        run["stage"] = "running_posted"

    def stage_running_posted(self, run, _changed):
        if not self.row_is(run, RUNNING_COUNTS):
            return self.maybe_repair(run, "running")
        self.post(run, "Done.xml", self.done_body(run))
        run["done_at"] = now()
        run["repairs"] = 0
        run["stage"] = "running_done"
        return NEXT

    def stage_running_done(self, run, changed):
        if changed:
            return STOP
        job = self.job_state(run)
        if job["kind"] != "finished":
            return STOP
        self.finish_job(run, job)
        if now() - run["done_at"] < self.timing["replace_gap_seconds"]:
            return STOP
        self.post(run, "Update.xml", self.update_body(run))
        run["stage"] = "final_update_posted"
        return CHANGED

    def stage_final_update_posted(self, run, _changed):
        if not self.row_is(run, REPLACED):
            return self.maybe_repair(run, "final")
        self.post(run, "Test.xml", self.final_test_xml(run))
        run["stage"] = "final_posted"
        return NEXT

    def stage_final_posted(self, run, _changed):
        if not self.row_is(run, tuple(run["expected"])):
            return self.maybe_repair(run, "final")
        self.post(run, "Done.xml", self.done_body(run))
        run["stage"] = "final_done"
        return NEXT

    def stage_final_done(self, run, _changed):
        self.archive(run)
        source = pathlib.Path(run["source_dir"])
        if source.exists() and not self.dry_run:
            shutil.rmtree(source)
        run["stage"] = "archived"
        log.info("%s %s closed: %s", run["name"], run["version"],
                 (run.get("verdict") or {}).get("reason"))
        return STOP

    def stage_repair_wait(self, run, changed):
        if changed or now() - run["repair_at"] < self.timing["replace_gap_seconds"]:
            return STOP
        if run["repair_kind"] == "running":
            job = self.job_state(run)
            if job["kind"] == "finished":
                self.finish_job(run, job)
                run["repair_kind"] = "final"
            else:
                self.post_running(run)
                return CHANGED
        self.post(run, "Update.xml", self.update_body(run))
        run["stage"] = "final_update_posted"
        return CHANGED

    # ----- read-back and repair

    def row_is(self, run, expected):
        """Poll the row until it matches, within what's left of this tick's waiting time."""
        while True:
            actual = self.cdash.row(self.site, run["name"], run["stamp"])
            if actual == tuple(expected):
                return True
            if self.dry_run or now() >= self.deadline:
                log.info("%s %s: row shows %s, waiting for %s",
                         run["name"], run["stage"], actual, tuple(expected))
                return False
            time.sleep(min(15, max(0, self.deadline - now())))

    def maybe_repair(self, run, kind):
        if now() - run["posted_at"] < self.timing["repair_after_seconds"]:
            return STOP
        run["repairs"] += 1
        if run["repairs"] <= self.timing["max_repairs"]:
            log.warning("%s row is wrong; repair %d: sending Done.xml, repost next tick",
                        run["name"], run["repairs"])
            self.post(run, "Done.xml", self.done_body(run))
            run["stage"] = "repair_wait"
            run["repair_kind"] = kind
            run["repair_at"] = now()
            return STOP
        if kind == "running":
            # The dashboard state is only informational here; the final step still has to work.
            log.error("%s: giving up on showing the running state", run["name"])
            run["done_at"] = now()
            run["repairs"] = 0
            run["stage"] = "running_done"
            return STOP
        if not run.get("fallback"):
            old_stamp = run["stamp"]
            stamp = stamp_for(now(), self.config["cdash"]["track"])
            run["stamp"] = stamp if stamp != old_stamp else stamp_for(
                now() + 60, self.config["cdash"]["track"])
            run["fallback"] = True
            run["repairs"] = 0
            log.error("%s: row %s could not be repaired and may look stale; "
                      "publishing the result as a new row %s", run["name"], old_stamp, run["stamp"])
            return self.post_final_fresh(run)
        log.error("%s %s: could not confirm the published result on CDash",
                  run["name"], run["version"])
        run["unconfirmed"] = True
        run["stage"] = "final_done"
        return NEXT

    # ----- archive

    def archive(self, run):
        settings = self.config.get("archive", {})
        if not settings.get("enabled"):
            return
        if self.dry_run:
            log.info("[dry run] would archive %s", run["job_name"])
            return
        clone = self.config["state_dir"] / "archive"
        branch = settings.get("branch", "results")
        env = dict(os.environ)
        if settings.get("ssh_key"):
            key = os.path.expanduser(settings["ssh_key"])
            env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes"
        git = ["git", "-c", "user.name=hpc-monitor", "-c", "user.email=hpc-monitor@localhost"]

        def call(*args, check=True):
            return self.shell([*git, "-C", str(clone), *args], check=check, env=env)

        try:
            if not (clone / ".git").exists():
                self.shell(["git", "clone", "--quiet", settings["repo"], str(clone)], env=env)
                remote = call("ls-remote", "--heads", "origin", branch)
                if remote.strip():
                    call("checkout", "--quiet", branch)
                else:
                    call("checkout", "--quiet", "--orphan", branch)
                    call("rm", "-rfq", "--ignore-unmatch", ".")
            job_id = run.get("job_id") or "not-submitted"
            relative = pathlib.Path("results", self.site, run["name"], run["version"], str(job_id))
            target = clone / relative
            target.mkdir(parents=True, exist_ok=True)
            run_dir = pathlib.Path(run["run_dir"])
            junit = run_dir / "junit.xml"
            if junit.exists():
                (target / "junit.xml").write_text(scrub(junit.read_text(errors="replace")))
            (target / "log_tail.txt").write_text(scrub(read_tail(run_dir / "slurm.out")))
            summary = {key: run.get(key) for key in (
                "name", "version", "versions", "stamp", "job_id", "job", "verdict", "setup_error",
                "unconfirmed",
            )} | {"machine": self.site}
            (target / "summary.json").write_text(scrub(json.dumps(summary, indent=1)))
            call("add", str(relative))
            call("commit", "--quiet", "-m",
                 f"{self.site} {run['name']} {run['version']} job {job_id}", check=False)
            for attempt in range(3):
                try:
                    remote = call("ls-remote", "--heads", "origin", branch)
                    if remote.strip():
                        call("pull", "--quiet", "--rebase", "origin", branch)
                    call("push", "--quiet", "origin", f"HEAD:{branch}")
                    break
                except RuntimeError:
                    if attempt == 2:
                        raise
        except (RuntimeError, OSError) as err:
            raise TransientError(f"archive failed: {err}") from err


# ------------------------------------------------------------------------------ CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    tick = commands.add_parser("tick", help="advance open runs; scheduled command")
    run = commands.add_parser("run", help="start a run of a chosen version now")
    run.add_argument("--version", required=True)
    run.add_argument("--run", help="[[run]] name, if the site has several")
    for sub in (tick, run):
        sub.add_argument("--site", required=True, help="path to site.toml")
        sub.add_argument("--dry-run", action="store_true", help="show actions, change nothing")
    args = parser.parse_args(argv)

    config = load_config(args.site)
    handlers = [logging.StreamHandler(sys.stdout)]
    if not args.dry_run:
        config["state_dir"].mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config["state_dir"] / "monitor.log"))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.dry_run:
        return run_command(args, config)

    # The lock is released by the OS when this process ends, however it ends.
    with open(config["state_dir"] / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another tick is running; exiting")
            return 0
        return run_command(args, config)


def run_command(args, config):
    monitor = Monitor(config, args.dry_run)
    if args.command == "tick":
        return monitor.tick()
    return monitor.manual_run(args.run, args.version)


if __name__ == "__main__":
    sys.exit(main())
