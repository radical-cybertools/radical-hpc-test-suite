#!/usr/bin/env python3
"""Test each new release on SLURM and publish the results to a git repo.

Short-lived and safe to repeat: each invocation advances open runs, publishes, and exits,
so no process stays behind on the machine. Grafana reads the published CSV files.

    hpc_monitor.py tick --site sites/<machine>/site.toml [--dry-run]
    hpc_monitor.py run  --site sites/<machine>/site.toml --version 0.6.0 [--dry-run]

A run moves through: submitting -> tracking -> closing -> closed. Publishing rebuilds the
machine's files from saved state, so a failed push is simply retried by the next tick.
"""

import argparse
import csv
import datetime
import fcntl
import getpass
import io
import json
import logging
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("hpc_monitor")

DEFAULT_TIMING = {
    "missing_grace_seconds": 3600,  # accounting can lag behind a fresh submission
    "heartbeat_seconds": 86400,  # how often an otherwise idle machine reports it is alive
    "install_timeout_seconds": 1500,
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

COLUMNS = (
    "run_id", "machine", "software", "version", "slurm_job_id", "status", "slurm_state",
    "failure_summary", "passed", "failed", "skipped", "duration_min", "nodes",
    "dragon_version", "submitted_at", "ended_at", "details_url", "last_reported",
    "cpu_percent", "peak_memory_gb", "cpu_hours",
)
TEST_COLUMNS = (
    "run_id", "machine", "version", "slurm_job_id", "run_status", "slurm_state", "passed",
    "failed", "skipped", "duration_min", "cpu_percent", "peak_memory_gb", "dragon_version", "test",
    "result", "outcome", "duration_s", "error", "details_url", "submitted_at",
)
USAGE_COLUMNS = (
    "run_id", "machine", "version", "slurm_job_id", "test_file", "elapsed_s", "cpu_percent",
    "peak_memory_gb", "submitted_at",
)
USAGE_FIELDS = ("JobID", "Elapsed", "TotalCPU", "CPUTime", "MaxRSS", "Start", "End")
RESULT_ORDER = {"FAILED": 0, "PASSED": 1, "SKIPPED": 2}
# Grafana can't colour cells inside a nested table, so the text carries the colour.
OUTCOMES = {"FAILED": "🔴 FAILED", "PASSED": "🟢 PASSED", "SKIPPED": "⚪ SKIPPED"}
MAX_SUMMARY_CHARS = 300
MAX_ERROR_CHARS = 2000
MAX_JUNIT_BYTES = 5 * 1024 * 1024
LOG_TAIL_LINES = 200
PUSH_ATTEMPTS = 4


class TransientError(Exception):
    """Something outside our control failed; the next tick retries from the same stage."""


def now():
    return time.time()


# --------------------------------------------------------------- public-safe text


def scrub(text, user=None, home=None):
    """Remove personal paths, secret-looking values, and control characters."""
    home = home if home is not None else os.path.expanduser("~")
    user = user if user is not None else getpass.getuser()
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    text = re.sub(r"[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]", "", text)
    text = re.sub(
        r"(?i)\b([A-Z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_.-]*)(\s*[=:]\s*)\S+",
        r"\1\2<redacted>", text,
    )
    text = re.sub(r"(?i)\b(bearer\s+)\S+", r"\1<redacted>", text)
    # Home can be a symlink, and logs can hold either spelling of it. The longer goes first,
    # so a spelling that starts with the other one is replaced whole.
    paths = {home, os.path.realpath(home)} if home else set()
    for path in sorted(paths - {"", "/"}, key=len, reverse=True):
        text = text.replace(path, "~")
    if user:
        # Paths outside home, such as /pscratch/sd/<letter>/<user>, also name the first letter.
        text = re.sub(rf"/{re.escape(user[0])}/{re.escape(user)}\b", "/<user>", text)
        text = re.sub(rf"\b{re.escape(user)}\b", "<user>", text)
    return text


def read_tail(path, lines=LOG_TAIL_LINES):
    try:
        text = pathlib.Path(path).read_text(errors="replace")
    except OSError:
        return "(no job log found)"
    return "\n".join(text.splitlines()[-lines:])


def one_line(text, limit=MAX_SUMMARY_CHARS):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def iso(timestamp):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def parse_slurm_time(value):
    """Return an epoch for a SLURM local timestamp, or None for Unknown and the like."""
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def elapsed_minutes(value):
    """Convert [D-]HH:MM:SS to minutes, or None if it can't be read."""
    match = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+)", value or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return round(days * 1440 + hours * 60 + minutes + seconds / 60, 1)


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
        output = "\n".join(part for e in problems for part in (e.get("message"), e.text) if part)
        message = next((e.get("message") for e in problems if e.get("message")), "")
        tests.append({
            "name": name, "status": "failed" if problems else "passed", "output": output,
            "message": message,
        })
    return tests, skipped


def junit_cases(path):
    """Return every test in a JUnit file with its result and its error or skip reason."""
    cases = []
    for case in ET.parse(path).getroot().iter("testcase"):
        problems = [e for e in case if e.tag in ("failure", "error")]
        skips = case.findall("skipped")
        detail = next(iter(problems + skips), None)
        error = "" if detail is None else (detail.get("message") or detail.text or "")
        seconds = float(case.get("time") or 0)
        cases.append({
            "test": ".".join(part for part in (case.get("classname"), case.get("name")) if part),
            "result": "FAILED" if problems else "SKIPPED" if skips else "PASSED",
            "duration_s": round(seconds, 1) if seconds > 0 else "",
            "error": error,
        })
    return cases


def slurm_seconds(value):
    """Convert sacct's [D-][H:]M:S[.f] to seconds, or None if it can't be read."""
    match = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)(\.\d+)?", value or "")
    if not match:
        return None
    days, hours, minutes, seconds, fraction = match.groups()
    return (int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
            + float(fraction or 0))


def memory_gb(value):
    """Convert sacct's memory, such as 7429420K, to GiB, or None if it can't be read."""
    match = re.fullmatch(r"([\d.]+)([KMGT]?)", value or "")
    if not match:
        return None
    return float(match[1]) * {"": 1024**-3, "K": 1024**-2, "M": 1024**-1, "G": 1, "T": 1024}[match[2]]


def summarize_usage(cpu_rows, memory_rows):
    """CPU use across cpu_rows, and the largest memory use of any single task in memory_rows."""
    used = sum(slurm_seconds(r["TotalCPU"]) or 0 for r in cpu_rows)
    allocated = sum(slurm_seconds(r["CPUTime"]) or 0 for r in cpu_rows)
    memory = [m for m in (memory_gb(r["MaxRSS"]) for r in memory_rows) if m is not None]
    return {
        "cpu_percent": round(100 * used / allocated, 1) if allocated else "",
        "cpu_hours": round(used / 3600, 2),
        "peak_memory_gb": round(max(memory), 2) if memory else "",
    }


def usage_per_file(steps, groups_dir):
    """Split a job's SLURM steps between test files, using when each file's log last changed."""
    logs = [p for p in groups_dir.glob("*.log") if not p.stem.endswith("-cleanup")]
    logs.sort(key=lambda p: p.stat().st_mtime)
    windows, start = [], float("-inf")
    for path in logs:
        windows.append((path.stem, start, path.stat().st_mtime))
        start = path.stat().st_mtime
    assigned = {name: [] for name, _, _ in windows}
    for step in steps:
        began, ended = parse_slurm_time(step["Start"]), parse_slurm_time(step["End"])
        if began is None or ended is None or not windows:
            continue
        overlaps = [(min(ended, stop) - max(began, first), name) for name, first, stop in windows]
        overlap, name = max(overlaps, key=lambda item: item[0])
        if overlap > 0:
            assigned[name].append(step)
    return [
        {"test_file": name,
         "elapsed_s": sum(slurm_seconds(s["Elapsed"]) or 0 for s in chosen),
         **{k: v for k, v in summarize_usage(chosen, chosen).items() if k != "cpu_hours"}}
        for name, chosen in assigned.items() if chosen
    ]


def decide(job, junit_path):
    """Turn the SLURM result and JUnit file into a verdict. Partial results are kept."""
    state = (job or {}).get("state", "UNKNOWN")
    try:
        tests, skipped = parse_junit(junit_path)
        junit_error = None
    except ValueError as err:
        tests, skipped, junit_error = [], [], str(err)

    failed = [t for t in tests if t["status"] == "failed"]
    executed = len(tests)

    if state == "UNKNOWN":
        ok, reason = False, "job is no longer known to SLURM accounting"
    elif state == "COMPLETED" and junit_error:
        ok, reason = False, f"job completed but no results: {junit_error}"
    elif state == "COMPLETED" and not executed:
        ok, reason = False, "job completed but no tests executed"
    elif state in ("COMPLETED", "FAILED") and failed:
        # pytest exits non-zero when a test fails, so this is a test result, not a crash.
        ok, reason = False, f"{len(failed)} of {executed} tests failed"
    elif state == "COMPLETED":
        ok, reason = True, f"{executed} tests passed"
    else:
        detail = (job or {}).get("reason") or ""
        detail = "" if detail in ("None", "") else f" ({detail})"
        ok, reason = False, f"Killed by SLURM: {state}{detail}"
        if junit_error and state != "FAILED":
            reason += "; no JUnit file written"
        elif junit_error:
            reason = f"SLURM ended the job: {state}{detail}; {junit_error}"

    return {
        "ok": ok, "reason": reason,
        "passed": executed - len(failed), "failed": len(failed), "skipped": len(skipped),
        "failed_tests": [{k: t[k] for k in ("name", "output", "message")} for t in failed],
        "skipped_tests": skipped,
    }


def last_test_seen(run_dir):
    """Best effort: the last test named in the logs, for jobs killed before reporting."""
    run_dir = pathlib.Path(run_dir)
    logs = sorted((run_dir / "groups").glob("*.log"), key=lambda p: p.stat().st_mtime)
    for path in [*reversed(logs), run_dir / "slurm.out"]:
        try:
            names = re.findall(r"[\w.-]+\.py::[\w\[\]\-.]+", path.read_text(errors="replace"))
        except OSError:
            continue
        if names:
            return names[-1]
    return None


def failure_summary(verdict, run_dir):
    """Build the single line that explains a failure on the dashboard."""
    if verdict["ok"]:
        return ""
    parts = [verdict["reason"]]
    failures = verdict["failed_tests"]
    if failures:
        first = failures[0]
        message = first["output"].strip().splitlines()[0] if first["output"].strip() else ""
        parts.append(f"{first['name']}: {message}" if message else first["name"])
        if len(failures) > 1:
            parts.append(f"and {len(failures) - 1} more")
    elif verdict["reason"].startswith("Killed by SLURM"):
        test = last_test_seen(run_dir)
        if test:
            parts.append(f"last test seen: {test}")
    return one_line(scrub("; ".join(parts)))


# --------------------------------------------------------------------------- config


def load_config(path):
    path = pathlib.Path(path).resolve()
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    for key in ("machine", "run", "publish"):
        if key not in config:
            raise SystemExit(f"{path}: missing required setting {key!r}")
    if "repo" not in config["publish"]:
        raise SystemExit(f"{path}: [publish] needs a repo")
    config["site_dir"] = path.parent
    config["site_file"] = path
    config["timing"] = DEFAULT_TIMING | config.get("timing", {})
    state_root = (os.environ.get("HPC_MONITOR_HOME") or config.get("state_dir")
                  or "~/.hpc-monitor-grafana")
    config["state_dir"] = pathlib.Path(state_root).expanduser() / config["machine"]
    config.setdefault("python", "python3")
    config.setdefault("setup", [])
    config["publish"].setdefault("branch", "main")
    config["publish"].setdefault("public_url", "")
    for run in config["run"]:
        for key in ("name", "package", "job_script"):
            if key not in run:
                raise SystemExit(f"{path}: a [[run]] entry is missing {key!r}")
        run.setdefault("software", run["package"].removesuffix("-py"))
        run.setdefault("install", f"{run['package']}[dragon]=={{version}}")
        run.setdefault("extra_packages", ["pytest", "pytest-asyncio"])
        run.setdefault("record_versions", ["dragonhpc"])
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
        self.state = {"runs": [], "tested": {}, "last_published": 0}
        if self.state_file.exists():
            self.state |= json.loads(self.state_file.read_text())

    # ----- plumbing

    def save(self):
        if self.dry_run:
            return
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=1))
        os.replace(temporary, self.state_file)

    def shell(self, command, check=True, env=None, cwd=None, timeout=None):
        """Run a command and return its stdout, raising RuntimeError on failure."""
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, env=env, cwd=cwd, check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(f"{command[0]} timed out after {timeout} s") from err
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
        return [r for r in self.state["runs"] if r["stage"] != "closed"]

    # ----- entry points

    def tick(self, results_only=False):
        """Follow open runs and publish. Unless results_only, also check for a new release."""
        failures = 0
        log.info("tick: %d open run(s)", len(self.open_runs()))
        for run in self.open_runs():
            failures += self.advance(run)
        if not results_only:
            self.check_releases()
        failures += self.publish()
        return 1 if failures else 0

    def manual_run(self, name, version):
        specs = [s for s in self.config["run"] if name in (None, s["name"])]
        if not specs:
            log.error("no [[run]] named %s", name)
            return 1
        run = self.start_run(specs[0], version)
        if run is None:
            return 0
        failures = self.advance(run)
        return 1 if failures + self.publish() else 0

    # ----- the release check

    def latest_version(self, spec):
        with urllib.request.urlopen(spec["pypi_url"], timeout=60) as response:
            return json.load(response)["info"]["version"]

    def check_releases(self):
        """Start a run for the newest release if it hasn't been tested."""
        latest = {}
        for spec in self.config["run"]:
            try:
                latest[spec["name"]] = self.latest_version(spec)
            except (OSError, ValueError, KeyError) as err:
                # Nothing changes, so the next tick tries again.
                log.warning("could not read latest %s version: %s", spec["package"], err)
                return
        for spec in self.config["run"]:
            version = latest[spec["name"]]
            tested = self.state["tested"].get(spec["name"])
            if tested is None:
                # First sight is recorded only: a baseline spends allocation, so it's manual.
                log.info("recording %s %s; use the run command for a baseline",
                         spec["name"], version)
                self.state["tested"][spec["name"]] = [version]
            elif version not in tested:
                log.info("new release to test: %s %s", spec["package"], version)
                run = self.start_run(spec, version)
                if run:
                    self.advance(run)
            else:
                log.info("no new release of %s (latest %s)", spec["package"], version)
        self.save()

    # ----- starting a run

    def start_run(self, spec, version):
        created = now()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(created))
        run_id = re.sub(r"[^\w.-]+", "-", f"{spec['name']}-{version}-{stamp}")
        run_dir = self.config["state_dir"] / "runs" / run_id
        run = {
            "run_id": run_id, "name": spec["name"], "software": spec["software"],
            "version": version, "job_name": "hpcgraf-" + run_id, "stage": "submitting",
            "created_at": created, "run_dir": str(run_dir), "job_id": None, "versions": {},
        }
        if self.dry_run:
            self.show_plan(spec, run)
            return None
        run_dir.mkdir(parents=True, exist_ok=True)
        tested = self.state["tested"].setdefault(spec["name"], [])
        if version not in tested:
            tested.append(version)
        self.state["runs"].append(run)
        self.save()
        return run

    def show_plan(self, spec, run):
        version = run["version"]
        log.info("[dry run] would create a fresh environment with %s in %s/venv",
                 self.config["python"], run["run_dir"])
        log.info("[dry run] would run: pip install %s %s",
                 spec["install"].format(version=version), " ".join(spec["extra_packages"]))
        log.info("[dry run] would clone %s at %s", spec["source_repo"],
                 spec["source_tag"].format(version=version))
        log.info("[dry run] would submit %s as %s",
                 self.config["site_dir"] / spec["job_script"], run["job_name"])
        log.info("[dry run] would publish to %s", self.config["publish"]["repo"])

    def installed(self, python, package):
        code = "import importlib.metadata as m, sys; print(m.version(sys.argv[1]))"
        try:
            return self.shell([str(python), "-c", code, package]).strip() or None
        except (RuntimeError, OSError):
            return None

    def in_setup(self, command):
        """Run a command after the site's setup lines, such as module load."""
        setup = " && ".join(self.config["setup"])
        if not setup:
            return command
        quoted = " ".join(f"'{part}'" for part in command)
        return ["bash", "-c", f"{setup} && {quoted}"]

    def prepare_environment(self, spec, run):
        """Build a new venv holding exactly what the release ships, plus the test tools."""
        run_dir = pathlib.Path(run["run_dir"])
        venv, source = run_dir / "venv", run_dir / "source"
        for path in (venv, source):
            if path.exists():
                shutil.rmtree(path)
        version = run["version"]
        tag = spec["source_tag"].format(version=version)
        self.shell(["git", "clone", "--quiet", "--depth", "1", "--branch", tag,
                    spec["source_repo"], str(source)])
        self.shell(self.in_setup([self.config["python"], "-m", "venv", str(venv)]))
        python = venv / "bin" / "python"
        wanted = [spec["install"].format(version=version), *spec["extra_packages"]]
        log.info("installing %s", " ".join(wanted))
        self.shell(self.in_setup([str(python), "-m", "pip", "install", "--quiet", *wanted]),
                   timeout=self.timing["install_timeout_seconds"])
        actual = self.installed(python, spec["package"])
        if actual != version:
            raise RuntimeError(f"environment has {spec['package']} {actual}, wanted {version}")
        run["versions"] = {
            p: self.installed(python, p) or "unknown" for p in spec["record_versions"]
        }
        run["versions"]["source commit"] = self.shell(
            ["git", "-C", str(source), "rev-parse", "--short=12", "HEAD"]).strip()

    def submit_job(self, spec, run):
        script = (self.config["site_dir"] / spec["job_script"]).resolve()
        if not script.exists():
            raise RuntimeError(f"job script not found: {script}")
        run_dir = pathlib.Path(run["run_dir"])
        env = os.environ | {
            "HPC_MONITOR_RUN_DIR": str(run_dir),
            "HPC_MONITOR_VENV": str(run_dir / "venv"),
            "HPC_MONITOR_SOURCE_DIR": str(run_dir / "source"),
            "HPC_MONITOR_VERSION": run["version"],
            "HPC_MONITOR_REPO_DIR": str(pathlib.Path(__file__).resolve().parent),
            "HPC_MONITOR_SITE_FILE": str(self.config["site_file"]),
            "HPC_MONITOR_PYTHON": sys.executable,
        }
        output = self.shell(
            ["sbatch", "--parsable", f"--job-name={run['job_name']}",
             f"--output={run_dir}/slurm.out", "--export=ALL", str(script)],
            env=env, cwd=str(run_dir),
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

    # ----- advancing a run

    def advance(self, run):
        """Move a run on as far as it can go now. Returns 1 if it should be retried."""
        try:
            if run["stage"] == "submitting":
                self.stage_submitting(run)
                self.save()
            if run["stage"] == "tracking":
                self.stage_tracking(run)
                self.save()
        except (TransientError, RuntimeError) as err:
            log.warning("%s %s, will retry next tick: %s", run["run_id"], run["stage"], err)
            self.save()
            return 1
        return 0

    def stage_submitting(self, run):
        spec = self.spec(run)
        job_id = self.find_submitted_job(run)
        if not job_id:
            try:
                self.prepare_environment(spec, run)
                job_id = self.submit_job(spec, run)
                log.info("%s submitted as job %s", run["run_id"], job_id)
            except (RuntimeError, OSError) as err:
                log.error("could not start %s: %s", run["run_id"], err)
                run["setup_error"] = str(err)
                self.finish(run)
                return
        run["job_id"] = job_id
        run["stage"] = "tracking"

    def stage_tracking(self, run):
        job = self.job_state(run)
        run["job"] = job
        if job["kind"] == "finished":
            self.finish(run)

    def finish(self, run):
        """Freeze the verdict once, so every publish retry reports the same result."""
        run_dir = pathlib.Path(run["run_dir"])
        if run.get("setup_error"):
            error = run["setup_error"].strip().splitlines()[-1] if run["setup_error"].strip() else ""
            verdict = {
                "ok": False, "reason": f"Could not start the job: {error}", "passed": None,
                "failed": None, "skipped": None, "failed_tests": [], "skipped_tests": [],
            }
        else:
            verdict = decide(run.get("job"), run_dir / "junit.xml")
        verdict["summary"] = failure_summary(verdict, run_dir)
        run["verdict"] = verdict
        run["finished_at"] = now()
        run["stage"] = "closing"
        log.info("%s: %s", run["run_id"], "PASSED" if verdict["ok"] else
                 f"FAILED: {verdict['summary']}")

    def close(self, run):
        """Called once a run's results are safely pushed."""
        run_dir = pathlib.Path(run["run_dir"])
        for path in (run_dir / "venv", run_dir / "source"):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        run["stage"] = "closed"
        log.info("%s closed", run["run_id"])

    # ----- dashboard data

    def status(self, run):
        verdict = run.get("verdict")
        if verdict:
            return "PASSED" if verdict["ok"] else "FAILED"
        if (run.get("job") or {}).get("kind") == "running":
            return "RUNNING"
        return "PENDING"

    def details_path(self, run):
        job = run.get("job_id") or "not-submitted"
        return pathlib.PurePosixPath(
            "data", "runs", self.site, run["software"], run["version"],
            f"{job}-{run['run_id'][-15:]}")

    def row(self, run):
        job = run.get("job") or {}
        verdict = run.get("verdict") or {}
        ended = parse_slurm_time(job.get("end"))
        if ended is None and verdict:
            ended = run.get("finished_at")
        public_url = self.config["publish"]["public_url"].rstrip("/")
        minutes = elapsed_minutes(job.get("elapsed")) if verdict else None
        state = job.get("state") or ("NOT_SUBMITTED" if run.get("setup_error") else "PENDING")
        values = {
            "run_id": run["run_id"], "machine": self.site, "software": run["software"],
            "version": run["version"], "slurm_job_id": run.get("job_id") or "",
            "status": self.status(run), "slurm_state": state,
            "failure_summary": verdict.get("summary", ""),
            "passed": verdict.get("passed"), "failed": verdict.get("failed"),
            "skipped": verdict.get("skipped"), "duration_min": minutes,
            "nodes": "" if job.get("nodes") == "None assigned" else job.get("nodes", ""),
            "dragon_version": run["versions"].get("dragonhpc", ""),
            "submitted_at": iso(run["created_at"]), "ended_at": iso(ended) if ended else "",
            "details_url": f"{public_url}/{self.details_path(run)}" if public_url else "",
            "last_reported": "",
        }
        usage = self.job_usage(run)
        for key in ("cpu_percent", "peak_memory_gb", "cpu_hours"):
            values[key] = usage.get(key)
        return {k: "" if v is None else str(v) for k, v in values.items()}

    def job_usage(self, run):
        """Return what SLURM recorded a finished job using, kept once it has been read."""
        if "usage" in run:
            return run["usage"]
        if not run.get("job_id") or not run.get("verdict"):
            return {}
        try:
            output = self.shell(["sacct", "-j", str(run["job_id"]), "--noheader", "--parsable2",
                                 f"--format={','.join(USAGE_FIELDS)}"])
        except (RuntimeError, OSError) as err:
            log.warning("could not read usage for job %s: %s", run["job_id"], err)
            return {}
        rows = [dict(zip(USAGE_FIELDS, line.split("|"), strict=False))
                for line in output.splitlines() if line.strip()]
        job = next((r for r in rows if r["JobID"] == str(run["job_id"])), None)
        if job is None:
            return {}  # Accounting hasn't caught up; the next tick tries again.
        steps = [r for r in rows if re.fullmatch(rf"{run['job_id']}\.\d+", r["JobID"])]
        run["usage"] = {
            **summarize_usage([job], steps),
            "files": usage_per_file(steps, pathlib.Path(run["run_dir"]) / "groups"),
        }
        return run["usage"]

    def test_rows(self, run):
        """One row per test in a finished run, or one row if a failed run names no test."""
        verdict = run.get("verdict") or {}
        if not verdict:
            return []
        row = self.row(run)
        common = {k: row[k] for k in (
            "run_id", "machine", "version", "slurm_job_id", "slurm_state", "passed", "failed",
            "skipped", "duration_min", "cpu_percent", "peak_memory_gb", "dragon_version",
            "details_url", "submitted_at")}
        common["run_status"] = row["status"]
        try:
            cases = junit_cases(pathlib.Path(run["run_dir"]) / "junit.xml")
        except (OSError, ET.ParseError):
            # The JUnit file is gone or damaged, so only the failures are still known.
            cases = [{"test": t["name"], "result": "FAILED",
                      "error": t.get("message") or t["output"].partition("\n")[0]}
                     for t in verdict["failed_tests"]]
        if not cases and not verdict["ok"]:
            cases = [{"test": "(whole run)", "result": "FAILED", "error": verdict["summary"]}]
        return [common | case | {"outcome": OUTCOMES[case["result"]],
                                 "error": scrub(case["error"]).strip()[:MAX_ERROR_CHARS]}
                for case in cases]

    def usage_rows(self, run):
        row = self.row(run)
        common = {k: row[k] for k in ("run_id", "machine", "version", "slurm_job_id", "submitted_at")}
        return [common | item for item in self.job_usage(run).get("files", [])]

    def write_table(self, path, columns, rows_of):
        """Rebuild a per-run table, keeping runs that lost state no longer knows."""
        known = {run["run_id"] for run in self.state["runs"]}
        rows = []
        if path.exists():
            with open(path, newline="", encoding="utf-8") as handle:
                rows = [r for r in csv.DictReader(handle) if r["run_id"] not in known]
        for run in self.state["runs"]:
            rows += rows_of(run)
        # Newest run first and, within a run, failures first.
        rows.sort(key=lambda r: (RESULT_ORDER.get(r.get("result"), 0),
                                 r.get("test") or r.get("test_file") or ""))
        rows.sort(key=lambda r: r["submitted_at"], reverse=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, columns, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def write_artifacts(self, clone, run):
        """Copy one finished run's scrubbed evidence into the data repo."""
        run_dir = pathlib.Path(run["run_dir"])
        target = clone / self.details_path(run)
        target.mkdir(parents=True, exist_ok=True)
        junit = run_dir / "junit.xml"
        if junit.exists() and junit.stat().st_size <= MAX_JUNIT_BYTES:
            (target / "junit.xml").write_text(scrub(junit.read_text(errors="replace")))
        (target / "log_tail.txt").write_text(scrub(read_tail(run_dir / "slurm.out")))
        for name in {t["name"].split(".")[0] for t in run["verdict"]["failed_tests"]}:
            log_file = run_dir / "groups" / f"{name}.log"
            if log_file.exists():
                (target / log_file.name).write_text(scrub(read_tail(log_file)))
        summary = {key: run.get(key) for key in (
            "run_id", "software", "version", "job_id", "versions", "job", "setup_error",
        )} | {"machine": self.site, "verdict": run["verdict"]}
        (target / "summary.json").write_text(scrub(json.dumps(summary, indent=1)))

    def merge_rows(self, path):
        """Combine state with rows already published, so lost state can't erase history."""
        rows = {}
        if path.exists():
            with open(path, newline="") as handle:
                rows = {r["run_id"]: r for r in csv.DictReader(handle)}
        previous = {k: {c: v for c, v in r.items() if c != "last_reported"}
                    for k, r in rows.items()}
        merged = dict(rows)
        for run in self.state["runs"]:
            merged[run["run_id"]] = self.row(run)
        current = {k: {c: r.get(c, "") for c in COLUMNS if c != "last_reported"}
                   for k, r in merged.items()}
        return sorted(merged.values(), key=lambda r: r["submitted_at"]), previous != current

    def write_csv(self, path, rows):
        reported = iso(now())
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, COLUMNS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row | {"last_reported": reported})
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as handle:
            handle.write(buffer.getvalue())

    # ----- publishing

    def git_env(self):
        env = dict(os.environ)
        key = self.config["publish"].get("ssh_key")
        if key:
            key = os.path.expanduser(key)
            env["GIT_SSH_COMMAND"] = (f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes "
                                      "-o StrictHostKeyChecking=accept-new")
        return env

    def git(self, clone, *args, check=True):
        return self.shell(
            ["git", "-c", "user.name=hpc-monitor", "-c", "user.email=hpc-monitor@localhost",
             "-c", "commit.gpgsign=false", "-C", str(clone), *args],
            check=check, env=self.git_env(),
        )

    def remote_has_branch(self, clone):
        branch = self.config["publish"]["branch"]
        return bool(self.git(clone, "ls-remote", "--heads", "origin", branch).strip())

    def prepare_clone(self):
        settings = self.config["publish"]
        clone = self.config["state_dir"] / "data-repo"
        branch = settings["branch"]
        if (clone / ".git").exists():
            # The site may have moved to another repo or branch; never publish to the old one.
            origin = self.git(clone, "remote", "get-url", "origin", check=False).strip()
            current = self.git(clone, "symbolic-ref", "--short", "HEAD", check=False).strip()
            if (origin, current) != (settings["repo"], branch):
                log.info("publishing target changed, cloning %s again", settings["repo"])
                shutil.rmtree(clone)
        if not (clone / ".git").exists():
            if clone.exists():
                shutil.rmtree(clone)
            self.shell(["git", "clone", "--quiet", settings["repo"], str(clone)],
                       env=self.git_env())
            if self.remote_has_branch(clone):
                self.git(clone, "fetch", "--quiet", "origin", branch)
                self.git(clone, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
            else:
                self.git(clone, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
                self.git(clone, "rm", "-rfq", "--cached", "--ignore-unmatch", ".")
        # Anything uncommitted came from a killed tick and is rebuilt below from state.
        self.git(clone, "rebase", "--abort", check=False)
        self.git(clone, "reset", "--quiet", check=False)
        self.git(clone, "checkout", "--quiet", "--", ".", check=False)
        self.git(clone, "clean", "-fdq")
        if self.remote_has_branch(clone):
            self.git(clone, "pull", "--quiet", "--rebase", "origin", branch)
        return clone

    def has_unpushed(self, clone):
        branch = self.config["publish"]["branch"]
        if not self.git(clone, "rev-parse", "--verify", "-q", "HEAD", check=False).strip():
            return False
        remote = self.git(clone, "rev-parse", "--verify", "-q", f"origin/{branch}",
                          check=False).strip()
        if not remote:
            return True
        return self.git(clone, "rev-list", "--count", f"{remote}..HEAD").strip() != "0"

    def push(self, clone):
        branch = self.config["publish"]["branch"]
        for attempt in range(PUSH_ATTEMPTS):
            try:
                if self.remote_has_branch(clone):
                    self.git(clone, "pull", "--quiet", "--rebase", "origin", branch)
                self.git(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
                self.git(clone, "fetch", "--quiet", "origin", branch)
                return
            except RuntimeError as err:
                self.git(clone, "rebase", "--abort", check=False)
                if attempt == PUSH_ATTEMPTS - 1:
                    raise TransientError(f"push failed: {err}") from err
                # Another machine probably pushed first. Their files never overlap with ours.
                time.sleep(random.uniform(0.5, 3))

    def publish(self):
        """Rebuild this machine's files and push them if anything changed. Returns 1 to retry."""
        if self.dry_run:
            log.info("[dry run] would publish %d run(s) to %s", len(self.state["runs"]),
                     self.config["publish"]["repo"])
            return 0
        closing = [r for r in self.state["runs"] if r["stage"] == "closing"]
        try:
            clone = self.prepare_clone()
            path = clone / "data" / f"{self.site}.csv"
            rows, changed = self.merge_rows(path)
            heartbeat_due = (now() - self.state["last_published"]
                             >= self.timing["heartbeat_seconds"])
            for run in closing:
                self.write_artifacts(clone, run)
            if changed or heartbeat_due or not path.exists():
                self.write_csv(path, rows)
            self.write_table(path.with_name(f"{self.site}-tests.csv"), TEST_COLUMNS, self.test_rows)
            self.write_table(path.with_name(f"{self.site}-usage.csv"), USAGE_COLUMNS, self.usage_rows)
            self.git(clone, "add", "-A")
            if self.git(clone, "status", "--porcelain").strip():
                self.git(clone, "commit", "--quiet", "-m", self.commit_message(rows))
            if self.has_unpushed(clone):
                self.push(clone)
                log.info("published %s", path.name)
                self.state["last_published"] = now()
        except (TransientError, RuntimeError, OSError) as err:
            log.warning("publishing failed, will retry next tick: %s", err)
            self.save()
            return 1
        for run in closing:
            self.close(run)
        self.save()
        return 0

    def commit_message(self, rows):
        latest = rows[-1] if rows else {}
        return (f"{self.site}: {latest.get('software', 'no runs')} {latest.get('version', '')} "
                f"{latest.get('status', '')}".strip())


# ------------------------------------------------------------------------------ CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    tick = commands.add_parser("tick", help="advance open runs and publish; scheduled command")
    tick.add_argument("--results-only", action="store_true",
                      help="follow open runs and publish, but don't check for a new release")
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
        return monitor.tick(results_only=args.results_only)
    return monitor.manual_run(args.run, args.version)


if __name__ == "__main__":
    sys.exit(main())
