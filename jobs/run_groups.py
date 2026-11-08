#!/usr/bin/env python3
"""Run each test file under its own launcher inside one allocation, and write one JUnit file.

pytest writes results only when it finishes, so isolating files means a crash or hang costs
that file and not the whole run. The JUnit file is rewritten after every file, so a job
killed by SLURM still leaves the results of the files it finished.

    python3 run_groups.py --suite <dir> --junit junit.xml --budget 300 --total 1980 \
        --last test_mpi.py -- -w slurm -t tcp

Everything after "--" goes to the launcher. The exit code is 0 once the JUnit file is
written, because failures are reported in that file. A launcher that crashes or runs over
budget appears as a failed test named "<file>.launcher". Tests it finished first are kept,
with their errors, using what the pytest_live plugin recorded.

Adapted from run_suite.py on RHAPSODY's refactor/hpc-test-groups branch, but it finds the
test files itself, so it works with what each release tag ships. Standard library only.
"""

import argparse
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

TAIL_LINES = 100
STOP_GRACE_SECONDS = 10
CLEANUP_TIMEOUT_SECONDS = 120
MIN_USEFUL_SECONDS = 60
RESULTS_WAIT_SECONDS = 15
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def read_tail(path):
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(lines[-TAIL_LINES:])


def read_cases(path):
    """Return a JUnit file's test cases, or None if it's missing or invalid."""
    try:
        return list(ET.parse(path).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return None


def wait_for_cases(path, seconds=RESULTS_WAIT_SECONDS):
    """Read a JUnit file written on another node, allowing for shared file system delays.

    pytest can write the file on a compute node just before the launcher returns, and this
    node may not see it yet. Listing the directory makes the file system look again.
    """
    deadline = time.monotonic() + seconds
    while True:
        cases = read_cases(path)
        if cases is not None or time.monotonic() >= deadline:
            return cases
        time.sleep(1)
        try:
            os.listdir(path.parent)
        except OSError:
            pass


def read_live_results(path, name):
    """Rebuild a stopped run from pytest_live's file. Return (cases, test still running)."""
    results = {}
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return [], None
    for line in lines:
        try:
            record = json.loads(line)
            test, outcome = record["test"], record["outcome"]
        except (ValueError, KeyError, TypeError):
            continue  # The last line can be cut short when the run is killed.
        if outcome is None:
            results.setdefault(test, None)
        elif results.get(test) is None or outcome != "passed":
            results[test] = record

    cases, running = [], None
    for test, result in results.items():
        short_name = test.split("::")[-1]
        if result is None:
            running = short_name
            continue
        case = ET.Element("testcase", classname=name, name=short_name, time="0")
        if result["outcome"] != "passed":
            kind = "skipped" if result["outcome"] == "skipped" else "failure"
            # XML can't hold control characters, and tracebacks may contain them.
            message = CONTROL_CHARACTERS.sub(" ", result.get("message") or result["outcome"])
            problem = ET.SubElement(case, kind, message=message)
            problem.text = CONTROL_CHARACTERS.sub(" ", result.get("details") or "")
        cases.append(case)
    return cases, running


def launcher_failure(name, reason, log_path=None):
    case = ET.Element("testcase", classname=name, name="launcher", time="0")
    failure = ET.SubElement(case, "failure", message=reason)
    failure.text = f"{reason}\n\nLog (end):\n{read_tail(log_path)}" if log_path else reason
    return case


def run_limited(command, budget, log_path, cwd, env):
    """Run command in its own process group. Return True if it finished within budget."""
    with open(log_path, "w") as log:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.wait(timeout=budget)
            return True
        except subprocess.TimeoutExpired:
            pass
        for sig, grace in ((signal.SIGTERM, STOP_GRACE_SECONDS), (signal.SIGKILL, None)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
    return False


def leftover_steps(job_id):
    output = subprocess.run(
        ["squeue", "-h", "-s", "-j", job_id, "-o", "%i"],
        capture_output=True, text=True, check=False,
    ).stdout
    return [s for s in output.split() if not s.endswith((".batch", ".extern", ".interactive"))]


def cancel_slurm_steps():
    """Cancel steps a stopped launcher left behind. They would keep the nodes' memory."""
    job_id = os.environ.get("SLURM_JOB_ID")
    if not job_id or not shutil.which("squeue"):
        return
    steps = leftover_steps(job_id)
    if steps:
        subprocess.run(["scancel", *steps], check=False)
    deadline = time.monotonic() + 60
    while steps and time.monotonic() < deadline:
        time.sleep(2)
        steps = leftover_steps(job_id)


def clean_up_dragon(log_path, cwd, env):
    """Clear what an abnormal end left, so the next file can start."""
    cancel_slurm_steps()
    # Dragon's default cleanup uses SSH between nodes, which many machines don't allow.
    # srun reaches every node without it.
    command = ["dragon-cleanup", "-s"]
    nodes = os.environ.get("SLURM_NNODES")
    if nodes and shutil.which("srun"):
        command = ["srun", "--overlap", f"-N{nodes}", "--ntasks-per-node=1", *command]
    try:
        run_limited(command, CLEANUP_TIMEOUT_SECONDS, log_path, cwd, env)
    except OSError as err:
        print(f"=== cleanup could not run: {err}", flush=True)


def run_file(test_file, launcher_args, budget, work_dir, env):
    """Run one test file and return its cases, including any launcher failure."""
    name = test_file.stem
    junit = work_dir / f"{name}.xml"
    log = work_dir / f"{name}.log"
    live = work_dir / f"{name}.live.jsonl"
    junit.unlink(missing_ok=True)
    live.unlink(missing_ok=True)
    # The monitor passes each [[run]]'s pytest_args, such as -m "not gpu", through the job.
    pytest_args = shlex.split(env.get("HPC_MONITOR_PYTEST_ARGS", ""))
    command = ["dragon", *launcher_args, sys.executable, "-m", "pytest", str(test_file), "-v",
               *pytest_args, "-p", "pytest_live", f"--junitxml={junit}"]
    print(f"=== {name}: budget {budget} s", flush=True)
    start = time.monotonic()
    finished = run_limited(command, budget, log, work_dir, env | {
        "HPC_MONITOR_LIVE_RESULTS": str(live)})
    cases = wait_for_cases(junit) if finished else read_cases(junit)

    # The launcher's exit code can't be used: dragon has returned 0 when the application
    # failed or was killed. Missing results are the reliable sign of a crash.
    problem = None
    if not finished:
        problem = f"{name} exceeded its budget of {budget} s"
    elif cases is None:
        problem = f"launcher ended without results for {name}"
    if problem:
        clean_up_dragon(work_dir / f"{name}-cleanup.log", work_dir, env)

    if problem and cases is None:
        cases, running = read_live_results(live, name)
        if running:
            problem += f"; {running} was still running"

    print(read_tail(log), flush=True)
    print(f"=== {name}: {time.monotonic() - start:.0f} s, {len(cases or [])} tests, "
          f"{problem or 'launcher ok'}", flush=True)
    cases = list(cases or [])
    if problem:
        cases.append(launcher_failure(name, problem, log))
    return cases


def write_junit(path, cases):
    suite = ET.Element(
        "testsuite", name="hpc", tests=str(len(cases)),
        failures=str(sum(c.find("failure") is not None for c in cases)),
        errors=str(sum(c.find("error") is not None for c in cases)),
        skipped=str(sum(c.find("skipped") is not None for c in cases)),
    )
    suite.extend(cases)
    root = ET.Element("testsuites")
    root.append(suite)
    temporary = pathlib.Path(f"{path}.tmp")
    ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(path)


def ordered_files(suite, last):
    """Sorted test files, with the ones named in last moved to the end, in that order."""
    files = sorted(suite.glob("test_*.py"))
    late = [suite / name for name in last if (suite / name).exists()]
    return [f for f in files if f not in late] + late


def parse_args(argv):
    launcher_args = []
    if "--" in argv:
        split = argv.index("--")
        argv, launcher_args = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True, type=pathlib.Path)
    parser.add_argument("--junit", required=True, type=pathlib.Path)
    parser.add_argument("--budget", type=int, default=300, help="seconds per test file")
    parser.add_argument("--total", type=int, default=1980,
                        help="seconds for everything; keep this below the job's time limit")
    parser.add_argument("--last", default="", help="comma-separated files to run last")
    args = parser.parse_args(argv)
    args.launcher_args = launcher_args
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    suite = args.suite.resolve()
    files = ordered_files(suite, [n for n in args.last.split(",") if n])
    if not files:
        print(f"no test files found in {suite}", file=sys.stderr)
        return 1

    junit = args.junit.resolve()
    work_dir = junit.parent / "groups"
    work_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ | {"RHAPSODY_HPC": "1"}
    # Workers import hpc_workers by name, so the suite directory must be on the path.
    # This script's directory is there for the pytest_live plugin.
    paths = [str(suite), str(pathlib.Path(__file__).resolve().parent), env.get("PYTHONPATH")]
    env["PYTHONPATH"] = os.pathsep.join(p for p in paths if p)

    deadline = time.monotonic() + args.total
    cases = []
    write_junit(junit, cases)
    for test_file in files:
        remaining = int(deadline - time.monotonic())
        if remaining < MIN_USEFUL_SECONDS:
            cases.append(launcher_failure(test_file.stem, f"{test_file.stem} did not run: "
                                          "the job's time was used up"))
        else:
            budget = min(args.budget, remaining)
            cases += run_file(test_file, args.launcher_args, budget, work_dir, env)
        write_junit(junit, cases)
    print(f"=== wrote {junit}: {len(cases)} test results", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
