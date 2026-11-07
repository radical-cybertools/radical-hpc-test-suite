"""Offline tests: fake SLURM, Python, and Dragon on PATH, and a local bare git repo."""

import calendar
import contextlib
import csv
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import hpc_monitor  # noqa: E402

ALL_PASS = """<?xml version="1.0"?><testsuites><testsuite tests="2">
<testcase classname="t" name="a"/><testcase classname="t" name="b"/>
<testcase classname="t" name="c"><skipped message="no gpu"/></testcase>
</testsuite></testsuites>"""
ONE_FAIL = """<?xml version="1.0"?><testsuites><testsuite tests="2">
<testcase classname="t" name="a"/>
<testcase classname="t" name="b"><failure message="TimeoutError: 0/1 tasks">trace</failure></testcase>
</testsuite></testsuites>"""
MIXED = """<?xml version="1.0"?><testsuites><testsuite tests="3">
<testcase classname="t" name="a"/>
<testcase classname="t" name="b"><failure message="TimeoutError: 0/1 tasks">trace</failure></testcase>
<testcase classname="t" name="c"><skipped message="no GPU"/></testcase>
</testsuite></testsuites>"""
ONLY_SKIPS = """<?xml version="1.0"?><testsuites><testsuite tests="1">
<testcase classname="t" name="a"><skipped message="no gpu"/></testcase></testsuite></testsuites>"""
FAKE_VENV_PYTHON = """#!/bin/bash
if [ "$1" = "-c" ]; then
    file="$FAKE/installed/$3"
    [ -e "$file" ] && cat "$file" || exit 1
    exit 0
fi
echo "$@" >> "$FAKE/pip.log"
[ -e "$FAKE/pip.fail" ] && { echo "ERROR: no matching distribution" >&2; exit 1; }
[ -e "$FAKE/pip.wrong" ] && exit 0
for spec in "$@"; do
    case "$spec" in *==*)
        name="${spec%%[*}"; name="${name%%=*}"
        echo "${spec##*==}" > "$FAKE/installed/$name";;
    esac
done
echo 0.14.1 > "$FAKE/installed/dragonhpc"
"""


def epoch(year, month, day, hour=12):
    return float(calendar.timegm((year, month, day, hour, 0, 0)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.fake = self.tmp / "fake"
        (self.fake / "installed").mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.script("sbatch", """
            echo "$@" >> "$FAKE/sbatch.log"
            env | grep '^HPC_MONITOR_' > "$FAKE/sbatch.env"
            [ -e "$FAKE/sbatch.fail" ] && { echo "sbatch: error: denied" >&2; exit 1; }
            echo 4242
        """)
        self.script("sacct", """
            [ -e "$FAKE/sacct.fail" ] && { echo "sacct: database down" >&2; exit 1; }
            case "$*" in *--name=*) cat "$FAKE/sacct.name" 2>/dev/null; exit 0;; esac
            case "$*" in *TotalCPU*) cat "$FAKE/sacct.usage" 2>/dev/null; exit 0;; esac
            cat "$FAKE/sacct.txt" 2>/dev/null
            exit 0
        """)
        (self.fake / "venv_python").write_text(FAKE_VENV_PYTHON)
        self.script("basepython", """
            mkdir -p "$3/bin"
            cp "$FAKE/venv_python" "$3/bin/python"
            chmod +x "$3/bin/python"
        """)

        self.source = self.tmp / "source_repo"
        self.git("init", "--quiet", str(self.source))
        (self.source / "suite.txt").write_text("suite")
        for version in ("0.6.0", "0.7.0", "0.8.0"):
            self.git("-C", str(self.source), "add", ".")
            self.git("-C", str(self.source), "commit", "--quiet", "--allow-empty", "-m", version)
            self.git("-C", str(self.source), "tag", f"v{version}")

        self.data = self.tmp / "data.git"
        self.git("init", "--quiet", "--bare", "-b", "main", str(self.data))

        self.pypi = self.tmp / "pypi.json"
        self.set_latest("0.6.0")

        env = unittest.mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake),
            "HPC_MONITOR_HOME": str(self.tmp / "state"),
        })
        env.start()
        self.addCleanup(env.stop)

        self.clock = [epoch(2027, 1, 15)]
        patcher = unittest.mock.patch.object(hpc_monitor, "now", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

        self.site = self.make_site("machine-a")

    def git(self, *args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.org",
             "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True,
        )

    def script(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def set_latest(self, version):
        self.pypi.write_text(json.dumps({"info": {"version": version}}))

    def make_site(self, machine, extra=""):
        directory = self.tmp / "sites" / machine
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "job.sbatch").write_text("#!/bin/bash\n")
        path = directory / "site.toml"
        path.write_text(f"""
machine = "{machine}"
python = "basepython"
{extra}
[publish]
repo = "{self.data}"
public_url = "https://example.org/tree/main"

[[run]]
name = "one"
package = "rhapsody-py"
job_script = "job.sbatch"
source_repo = "file://{self.source}"
pypi_url = "file://{self.pypi}"
""")
        return path

    def tick(self, site=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return hpc_monitor.main(["tick", "--site", str(site or self.site)])

    def run_version(self, version="0.6.0", extra=()):
        with contextlib.redirect_stdout(io.StringIO()):
            return hpc_monitor.main(
                ["run", "--site", str(self.site), "--version", version, *extra])

    def state(self, machine="machine-a"):
        return json.loads((self.tmp / "state" / machine / "state.json").read_text())

    def record(self, index=-1):
        return self.state()["runs"][index]

    def set_job(self, state, reason="None", elapsed="00:10:00"):
        (self.fake / "sacct.txt").write_text(
            f"4242|{state}|0:0|{elapsed}|2027-01-15T08:00:00|2027-01-15T08:10:00|n1|{reason}\n")

    def write_junit(self, text):
        (pathlib.Path(self.record()["run_dir"]) / "junit.xml").write_text(text)

    def published(self, machine="machine-a"):
        """Read a machine's rows from the remote, as Grafana would."""
        clone = self.tmp / "reader"
        shutil.rmtree(clone, ignore_errors=True)
        self.git("clone", "--quiet", str(self.data), str(clone))
        with open(clone / "data" / f"{machine}.csv", newline="") as handle:
            return list(csv.DictReader(handle)), clone

    def commits(self):
        result = subprocess.run(["git", "-C", str(self.data), "rev-list", "--count", "main"],
                                capture_output=True, text=True, check=True)
        return int(result.stdout)

    def finished_run(self, state="COMPLETED", junit=ALL_PASS, **job):
        """Run a job to its end and return the published row."""
        (self.fake / "sacct.txt").unlink(missing_ok=True)
        self.assertEqual(self.run_version(), 0)
        self.set_job(state, **job)
        if junit is not None:
            self.write_junit(junit)
        self.tick()
        return self.published()[0][-1]


class Scrubbing(unittest.TestCase):
    def test_personal_values_and_secrets_are_removed(self):
        text = hpc_monitor.scrub("/home/me/x me API_TOKEN=abc Bearer xyz \x1b[31mred",
                                 user="me", home="/home/me")
        self.assertEqual(text, "~/x <user> API_TOKEN=<redacted> Bearer <redacted> red")

    def test_symlinked_home_and_its_real_path_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            real = root / "storage" / "person"
            real.mkdir(parents=True)
            (root / "homes").mkdir()
            home = root / "homes" / "person"
            home.symlink_to(real)
            text = hpc_monitor.scrub(f"{home}/a.py {real}/b.py", user="", home=str(home))
        self.assertEqual(text, "~/a.py ~/b.py")

    def test_home_whose_real_path_starts_with_it_is_removed_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            real = root / "person.real"
            real.mkdir()
            home = root / "person"
            home.symlink_to(real)
            text = hpc_monitor.scrub(f"{real}/a.py", user="", home=str(home))
        self.assertEqual(text, "~/a.py")

    def test_scratch_path_hides_the_letter_and_the_user(self):
        text = hpc_monitor.scrub("/pscratch/sd/p/person/run/x.log", user="person", home="/nowhere")
        self.assertEqual(text, "/pscratch/sd/<user>/run/x.log")

    def test_non_ascii_text_survives(self):
        self.assertEqual(hpc_monitor.scrub("naïve\x00", user="", home=""), "naïve")


class Outcomes(Base):
    def test_passed(self):
        row = self.finished_run()
        self.assertEqual((row["status"], row["passed"], row["failed"], row["skipped"]),
                         ("PASSED", "2", "0", "1"))
        self.assertEqual(row["failure_summary"], "")
        self.assertEqual(row["slurm_job_id"], "4242")
        self.assertEqual(row["duration_min"], "10.0")

    def test_failed_test_names_the_test_and_message(self):
        row = self.finished_run("FAILED", ONE_FAIL)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["failure_summary"],
                         "1 of 2 tests failed; t.b: TimeoutError: 0/1 tasks")

    def test_jobs_killed_by_slurm_show_as_failed(self):
        for state in ("TIMEOUT", "OUT_OF_MEMORY", "CANCELLED", "NODE_FAIL"):
            with self.subTest(state=state):
                row = self.finished_run(state, None)
                self.assertEqual(row["status"], "FAILED")
                self.assertIn(f"Killed by SLURM: {state}", row["failure_summary"])
                self.assertEqual(row["slurm_state"], state)
                self.clock[0] += 60

    def test_killed_job_names_last_test_and_keeps_partial_results(self):
        self.run_version()
        run_dir = pathlib.Path(self.record()["run_dir"])
        (run_dir / "groups").mkdir()
        (run_dir / "groups" / "test_scale.log").write_text(
            "x/test_mpi.py::test_a PASSED\nx/test_scale.py::test_big \n")
        self.write_junit(ALL_PASS)
        self.set_job("TIMEOUT")
        self.tick()
        row = self.published()[0][-1]
        self.assertEqual(row["passed"], "2")
        self.assertEqual(row["failure_summary"],
                         "Killed by SLURM: TIMEOUT; last test seen: test_scale.py::test_big")

    def test_job_missing_from_accounting_fails_after_grace(self):
        self.run_version()
        self.tick()
        self.assertEqual(self.published()[0][-1]["status"], "PENDING")
        self.clock[0] += 3601
        self.tick()
        row = self.published()[0][-1]
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("no longer known", row["failure_summary"])

    def test_completed_without_junit_fails(self):
        row = self.finished_run("COMPLETED", None)
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("JUnit file is missing", row["failure_summary"])

    def test_corrupt_junit_fails(self):
        row = self.finished_run("COMPLETED", "<testsuites><oops")
        self.assertIn("corrupt", row["failure_summary"])

    def test_only_skips_fails(self):
        row = self.finished_run("COMPLETED", ONLY_SKIPS)
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("no tests executed", row["failure_summary"])

    def test_pending_then_running_states(self):
        self.run_version()
        self.set_job("PENDING")
        self.tick()
        self.assertEqual(self.published()[0][-1]["status"], "PENDING")
        self.set_job("RUNNING")
        self.tick()
        self.assertEqual(self.published()[0][-1]["status"], "RUNNING")

    def test_pip_failure_is_reported_and_no_job_submitted(self):
        (self.fake / "pip.fail").touch()
        self.assertEqual(self.run_version(), 0)
        self.assertFalse((self.fake / "sbatch.log").exists())
        row = self.published()[0][-1]
        self.assertEqual((row["status"], row["slurm_state"]), ("FAILED", "NOT_SUBMITTED"))
        self.assertIn("Could not start the job: ERROR: no matching", row["failure_summary"])

    def test_wrong_installed_version_is_a_failure(self):
        (self.fake / "pip.wrong").touch()
        self.run_version()
        self.assertIn("wanted 0.6.0", self.published()[0][-1]["failure_summary"])

    def test_sbatch_failure_is_reported(self):
        (self.fake / "sbatch.fail").touch()
        self.run_version()
        self.assertIn("denied", self.published()[0][-1]["failure_summary"])

    def test_missing_tag_is_reported(self):
        self.run_version("9.9.9")
        self.assertEqual(self.published()[0][-1]["status"], "FAILED")


class Environment(Base):
    def test_fresh_environment_installs_the_release_and_records_dragon(self):
        self.run_version()
        pip = (self.fake / "pip.log").read_text()
        self.assertIn("rhapsody-py[dragon]==0.6.0", pip)
        run = self.record()
        self.assertEqual(run["versions"]["dragonhpc"], "0.14.1")
        env = (self.fake / "sbatch.env").read_text()
        self.assertIn(f"HPC_MONITOR_VENV={run['run_dir']}/venv", env)
        self.assertIn("HPC_MONITOR_VERSION=0.6.0", env)
        self.assertTrue((pathlib.Path(run["run_dir"]) / "source" / "suite.txt").exists())

    def test_environment_and_source_removed_when_closed(self):
        self.finished_run()
        run_dir = pathlib.Path(self.record()["run_dir"])
        self.assertFalse((run_dir / "venv").exists())
        self.assertFalse((run_dir / "source").exists())
        self.assertEqual(self.record()["stage"], "closed")

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.run_version(extra=("--dry-run",)), 0)
        self.assertFalse((self.fake / "sbatch.log").exists())
        self.assertFalse((self.tmp / "state" / "machine-a").exists())
        self.assertEqual(self.commits_or_zero(), 0)

    def commits_or_zero(self):
        result = subprocess.run(["git", "-C", str(self.data), "rev-list", "--count", "main"],
                                capture_output=True, text=True, check=False)
        return int(result.stdout or 0)


class ReleaseCheck(Base):
    def sbatches(self):
        path = self.fake / "sbatch.log"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def finish_latest_job(self):
        self.set_job("COMPLETED")
        self.write_junit(ALL_PASS)

    def test_first_sight_records_without_running(self):
        self.tick()
        self.assertEqual(self.sbatches(), 0)
        self.assertEqual(self.state()["tested"], {"one": ["0.6.0"]})

    def test_new_release_runs_once(self):
        self.tick()
        self.clock[0] = epoch(2027, 2, 1)
        self.set_latest("0.7.0")
        self.tick()
        self.assertEqual(self.sbatches(), 1)
        self.assertEqual(self.record()["version"], "0.7.0")
        self.finish_latest_job()
        self.clock[0] = epoch(2027, 2, 15)
        self.tick()
        self.assertEqual(self.sbatches(), 1)

    def test_two_releases_in_a_month_are_both_tested(self):
        self.tick()
        self.clock[0] = epoch(2027, 2, 1)
        self.set_latest("0.7.0")
        self.tick()
        self.finish_latest_job()
        self.clock[0] = epoch(2027, 2, 15)
        self.set_latest("0.8.0")
        self.tick()
        self.assertEqual(self.sbatches(), 2)
        self.assertEqual(self.record()["version"], "0.8.0")

    def test_only_the_newest_release_is_tested(self):
        self.tick()
        self.clock[0] = epoch(2027, 2, 1)
        self.set_latest("0.8.0")
        self.tick()
        self.assertEqual([r["version"] for r in self.state()["runs"]], ["0.8.0"])

    def test_no_new_release_means_no_run(self):
        self.tick()
        self.clock[0] = epoch(2027, 2, 1)
        self.tick()
        self.assertEqual(self.sbatches(), 0)

    def test_pypi_failure_retries_next_tick(self):
        self.tick()
        self.clock[0] = epoch(2027, 2, 1)
        self.pypi.unlink()
        self.tick()
        self.assertEqual(self.sbatches(), 0)
        self.set_latest("0.7.0")
        self.tick()
        self.assertEqual(self.sbatches(), 1)

    def test_baseline_counts_as_tested(self):
        self.run_version("0.6.0")
        self.finish_latest_job()
        self.clock[0] = epoch(2027, 2, 1)
        self.tick()
        self.assertEqual(self.sbatches(), 1)

    def test_state_with_the_old_monthly_key_still_loads(self):
        self.tick()
        state_file = self.tmp / "state" / "machine-a" / "state.json"
        state = json.loads(state_file.read_text())
        state["checked_month"] = "2027-01"
        state_file.write_text(json.dumps(state))
        self.set_latest("0.7.0")
        self.tick()
        self.assertEqual(self.sbatches(), 1)

    def results_only_tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return hpc_monitor.main(["tick", "--results-only", "--site", str(self.site)])

    def test_results_only_tick_never_starts_a_job(self):
        self.tick()
        self.set_latest("0.7.0")
        self.assertEqual(self.results_only_tick(), 0)
        self.assertEqual(self.sbatches(), 0)
        self.assertEqual(self.state()["runs"], [])

    def test_results_only_tick_publishes_a_finished_job(self):
        self.tick()
        self.set_latest("0.7.0")
        self.tick()
        self.finish_latest_job()
        self.set_latest("0.8.0")
        self.assertEqual(self.results_only_tick(), 0)
        rows, _ = self.published()
        self.assertEqual((rows[-1]["version"], rows[-1]["status"]), ("0.7.0", "PASSED"))
        self.assertEqual(self.sbatches(), 1)

    def test_job_is_told_how_to_publish(self):
        self.tick()
        self.set_latest("0.7.0")
        self.tick()
        environment = (self.fake / "sbatch.env").read_text()
        self.assertIn(f"HPC_MONITOR_SITE_FILE={self.site.resolve()}", environment)
        self.assertIn("HPC_MONITOR_PYTHON=", environment)


class PublishAfterJob(unittest.TestCase):
    """The script a job calls as it ends, with a fake ssh."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "ssh.log"
        self.fake_ssh(0)
        self.run_dir = self.tmp / "it's a run"
        self.run_dir.mkdir()

    def fake_ssh(self, code):
        ssh = self.bin / "ssh"
        ssh.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{self.calls}"\nexit {code}\n')
        ssh.chmod(0o755)

    def run_script(self, **extra):
        env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}", "HPC_MONITOR_LOGIN_HOST": "login1",
            "HPC_MONITOR_RUN_DIR": str(self.run_dir), "HPC_MONITOR_PYTHON": "/usr/bin/python3",
            "HPC_MONITOR_REPO_DIR": "/repo", "HPC_MONITOR_SITE_FILE": "/repo/it's/site.toml",
        } | extra
        env.pop("HPC_MONITOR_HOME", None)
        return subprocess.run(["bash", str(REPO / "jobs" / "publish_after_job.sh")], env=env,
                              capture_output=True, text=True, timeout=30, check=False)

    def test_asks_the_login_node_for_a_delayed_results_only_tick(self):
        result = self.run_script(HPC_MONITOR_PUBLISH_DELAY="7")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.calls.read_text()
        self.assertIn("login1", call)
        self.assertIn("sleep 7", call)
        self.assertIn("tick --results-only", call)
        self.assertIn("nohup setsid", call)

    def test_quoted_command_survives_the_remote_shell(self):
        self.run_script(HPC_MONITOR_PUBLISH_DELAY="0", HPC_MONITOR_PYTHON="/bin/echo")
        call = self.calls.read_text().strip()
        remote = call[call.index("sh -c "):call.index(" >/dev/null")]
        output = subprocess.run(["bash", "-c", remote], capture_output=True, text=True,
                                check=True).stdout
        self.assertEqual(output.strip(),
                         "/repo/hpc_monitor.py tick --results-only --site /repo/it's/site.toml")

    def test_runs_only_once(self):
        self.run_script()
        self.run_script()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_failed_connection_does_not_fail_the_job(self):
        self.fake_ssh(255)
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertIn("next scheduled tick", result.stdout)

    def test_no_login_host_does_not_fail_the_job(self):
        result = self.run_script(HPC_MONITOR_LOGIN_HOST="")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())


class Resume(Base):
    def test_lost_job_id_is_recovered_without_resubmitting(self):
        self.run_version()
        state = self.state()
        state["runs"][-1].update(stage="submitting", job_id=None)
        (self.tmp / "state" / "machine-a" / "state.json").write_text(json.dumps(state))
        (self.fake / "sacct.name").write_text("4242\n")
        self.set_job("RUNNING")
        before = (self.fake / "sbatch.log").read_text()
        self.tick()
        self.assertEqual((self.fake / "sbatch.log").read_text(), before)
        self.assertEqual(self.record()["job_id"], "4242")

    def test_sacct_outage_keeps_the_run_open(self):
        self.run_version()
        (self.fake / "sacct.fail").touch()
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.record()["stage"], "tracking")
        (self.fake / "sacct.fail").unlink()
        self.set_job("COMPLETED")
        self.write_junit(ALL_PASS)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.record()["stage"], "closed")

    def test_lock_makes_a_second_tick_exit(self):
        import fcntl
        directory = self.tmp / "state" / "machine-a"
        directory.mkdir(parents=True)
        with open(directory / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.tick(), 0)
            self.assertFalse((directory / "state.json").exists())


class Publishing(Base):
    def test_artifacts_are_scrubbed_and_row_links_to_them(self):
        self.run_version()
        run_dir = pathlib.Path(self.record()["run_dir"])
        (run_dir / "slurm.out").write_text(f"log from {os.path.expanduser('~')} TOKEN=abc\n")
        self.set_job("FAILED")
        self.write_junit(ONE_FAIL)
        self.tick()
        rows, clone = self.published()
        path = rows[-1]["details_url"].split("/tree/main/")[1]
        text = (clone / path / "log_tail.txt").read_text()
        self.assertIn("TOKEN=<redacted>", text)
        self.assertNotIn(os.path.expanduser("~"), text)
        ET.parse(clone / path / "junit.xml")
        self.assertFalse(json.loads((clone / path / "summary.json").read_text())["verdict"]["ok"])

    def test_no_commit_when_nothing_changed(self):
        self.finished_run()
        before = self.commits()
        self.clock[0] += 900
        self.tick()
        self.assertEqual(self.commits(), before)

    def test_daily_heartbeat_updates_last_reported(self):
        self.finished_run()
        before = self.commits()
        old = self.published()[0][-1]["last_reported"]
        self.clock[0] += 86400
        self.tick()
        self.assertEqual(self.commits(), before + 1)
        self.assertGreater(self.published()[0][-1]["last_reported"], old)

    def test_failed_push_keeps_run_closing_then_retries(self):
        self.run_version()
        self.set_job("COMPLETED")
        self.write_junit(ALL_PASS)
        hidden = self.data.with_suffix(".hidden")
        self.data.rename(hidden)
        with unittest.mock.patch.object(hpc_monitor.random, "uniform", lambda a, b: 0):
            self.assertEqual(self.tick(), 1)
        self.assertEqual(self.record()["stage"], "closing")
        self.assertTrue((pathlib.Path(self.record()["run_dir"]) / "venv").exists())
        hidden.rename(self.data)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.record()["stage"], "closed")
        self.assertEqual(self.published()[0][-1]["status"], "PASSED")

    def test_lost_state_does_not_erase_published_history(self):
        self.finished_run()
        first = self.published()[0][-1]["run_id"]
        shutil.rmtree(self.tmp / "state" / "machine-a")
        self.clock[0] += 3600
        self.run_version("0.7.0")
        ids = [r["run_id"] for r in self.published()[0]]
        self.assertIn(first, ids)
        self.assertEqual(len(ids), 2)

    def test_two_machines_pushing_at_once_both_land(self):
        monitors = []
        for machine in ("machine-a", "machine-b"):
            site = self.make_site(machine)
            config = hpc_monitor.load_config(site)
            config["state_dir"].mkdir(parents=True)
            monitors.append(hpc_monitor.Monitor(config))
        results = []

        def publish(monitor):
            for round_number in range(4):
                monitor.state["runs"].append({
                    "run_id": f"r{round_number}", "name": "one", "software": "rhapsody",
                    "version": f"0.{round_number}", "stage": "tracking", "job_id": str(round_number),
                    "created_at": hpc_monitor.now(), "run_dir": "/nonexistent", "versions": {},
                })
                results.append(monitor.publish())

        threads = [threading.Thread(target=publish, args=(m,)) for m in monitors]
        with self.assertLogs("hpc_monitor", level="INFO"):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, [0] * 8)
        self.assertEqual(len(self.published("machine-a")[0]), 4)
        self.assertEqual(len(self.published("machine-b")[0]), 4)

    def test_rows_sort_oldest_first_and_have_every_column(self):
        self.finished_run()
        self.clock[0] += 3600
        self.run_version("0.7.0")
        rows = self.published()[0]
        self.assertEqual([r["version"] for r in rows], ["0.6.0", "0.7.0"])
        self.assertEqual(tuple(rows[0]), hpc_monitor.COLUMNS)


    def published_tests(self, clone, machine="machine-a"):
        with open(clone / "data" / f"{machine}-tests.csv", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_every_test_is_published_with_failures_first(self):
        self.finished_run("COMPLETED", MIXED)
        _, clone = self.published()
        rows = self.published_tests(clone)
        self.assertEqual([(r["test"], r["result"]) for r in rows], [
            ("t.b", "FAILED"), ("t.a", "PASSED"), ("t.c", "SKIPPED")])
        self.assertEqual(rows[0]["error"], "TimeoutError: 0/1 tasks")
        self.assertEqual(rows[2]["error"], "no GPU")
        self.assertEqual(rows[0]["outcome"], "🔴 FAILED")

    def test_newest_run_is_listed_first(self):
        self.finished_run("COMPLETED", ALL_PASS)
        self.clock[0] += 60
        self.finished_run("COMPLETED", ONE_FAIL)
        _, clone = self.published()
        rows = self.published_tests(clone)
        self.assertEqual(rows[0]["result"], "FAILED")
        self.assertGreater(rows[0]["submitted_at"], rows[-1]["submitted_at"])

    def test_run_killed_without_results_still_has_a_row(self):
        self.assertEqual(self.run_version(), 0)
        self.set_job("TIMEOUT")
        self.tick()
        _, clone = self.published()
        rows = self.published_tests(clone)
        self.assertEqual((rows[0]["test"], rows[0]["result"]), ("(whole run)", "FAILED"))
        self.assertIn("TIMEOUT", rows[0]["error"])

    def test_missing_junit_still_lists_known_failures(self):
        self.finished_run("COMPLETED", ONE_FAIL)
        (pathlib.Path(self.record()["run_dir"]) / "junit.xml").unlink()
        monitor = hpc_monitor.Monitor(hpc_monitor.load_config(self.site))
        rows = monitor.test_rows(monitor.state["runs"][-1])
        self.assertEqual([(r["test"], r["result"]) for r in rows], [("t.b", "FAILED")])

    def test_test_list_survives_lost_state(self):
        self.finished_run("COMPLETED", ONE_FAIL)
        pathlib.Path(self.record()["run_dir"]).parent.parent.joinpath("state.json").unlink()
        self.assertEqual(self.tick(), 0)
        monitor = hpc_monitor.Monitor(hpc_monitor.load_config(self.site))
        clone = monitor.prepare_clone()
        self.assertEqual([r["test"] for r in self.published_tests(clone)], ["t.b", "t.a"])

    def test_time_and_run_details_are_on_each_test_row(self):
        self.finished_run("COMPLETED", """<testsuites><testsuite>
<testcase classname="t" name="a" time="2.345"/><testcase classname="t" name="b" time="0"/>
</testsuite></testsuites>""")
        _, clone = self.published()
        rows = {r["test"]: r for r in self.published_tests(clone)}
        self.assertEqual(rows["t.a"]["duration_s"], "2.3")
        self.assertEqual(rows["t.b"]["duration_s"], "")
        self.assertEqual((rows["t.a"]["run_status"], rows["t.a"]["passed"]), ("PASSED", "2"))
        self.assertEqual(rows["t.a"]["duration_min"], "10.0")
        for column in ("cpu_percent", "peak_memory_gb", "dragon_version"):
            self.assertIn(column, rows["t.a"])

    def test_usage_is_published_per_run_and_per_file(self):
        (self.fake / "sacct.usage").write_text(
            "4242|00:10:00|02:30:00|10:00:00||2027-01-15T08:00:00|2027-01-15T08:10:00\n"
            "4242.batch|00:10:00|00:10.000|05:00:00|900G|2027-01-15T08:00:00|2027-01-15T08:10:00\n"
            "4242.0|00:02:00|30:00|02:00:00|1048576K|2027-01-15T08:00:00|2027-01-15T08:02:00\n"
            "4242.1|00:03:00|01:30:00|03:00:00|2G|2027-01-15T08:02:00|2027-01-15T08:05:00\n")
        self.assertEqual(self.run_version(), 0)
        groups = pathlib.Path(self.record()["run_dir"]) / "groups"
        groups.mkdir(exist_ok=True)
        for name, stamp in (("test_a", "2027-01-15T08:02:00"), ("test_b", "2027-01-15T08:05:00")):
            (groups / f"{name}.log").write_text("log")
            moment = hpc_monitor.parse_slurm_time(stamp)
            os.utime(groups / f"{name}.log", (moment, moment))
        (groups / "test_b-cleanup.log").write_text("log")
        self.set_job("COMPLETED")
        self.write_junit(ALL_PASS)
        self.tick()
        rows, clone = self.published()
        self.assertEqual((rows[-1]["cpu_percent"], rows[-1]["peak_memory_gb"]), ("25.0", "2.0"))
        self.assertEqual(rows[-1]["cpu_hours"], "2.5")
        with open(clone / "data" / "machine-a-usage.csv", newline="") as handle:
            files = {r["test_file"]: r for r in csv.DictReader(handle)}
        self.assertEqual(sorted(files), ["test_a", "test_b"])
        self.assertEqual((files["test_a"]["cpu_percent"], files["test_a"]["peak_memory_gb"]),
                         ("25.0", "1.0"))
        self.assertEqual(files["test_b"]["elapsed_s"], "180.0")

    def test_usage_retries_while_accounting_catches_up(self):
        self.finished_run("COMPLETED", ALL_PASS)
        self.assertNotIn("usage", self.record())

    def test_failed_file_log_is_published_scrubbed(self):
        self.assertEqual(self.run_version(), 0)
        groups = pathlib.Path(self.record()["run_dir"]) / "groups"
        groups.mkdir(exist_ok=True)
        (groups / "t.log").write_text(f"from {os.path.expanduser('~')}\n")
        self.set_job("COMPLETED")
        self.write_junit(ONE_FAIL)
        self.tick()
        rows, clone = self.published()
        self.assertTrue(rows[-1]["details_url"].split("/tree/main/")[1].startswith("data/runs/"))
        path = rows[-1]["details_url"].split("/tree/main/")[1]
        self.assertNotIn(os.path.expanduser("~"), (clone / path / "t.log").read_text())

    def seed_code_branch(self):
        work = self.tmp / "code"
        work.mkdir()
        (work / "code.txt").write_text("code\n")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", "-C", str(work)]
        for args in (["init", "--quiet", "-b", "code"], ["add", "-A"],
                     ["commit", "--quiet", "-m", "code"],
                     ["push", "--quiet", str(self.data), "code"]):
            subprocess.run([*git, *args], check=True)
        subprocess.run(["git", "-C", str(self.data), "symbolic-ref", "HEAD", "refs/heads/code"],
                       check=True)

    def test_results_branch_starts_empty_and_leaves_code_branches_alone(self):
        self.seed_code_branch()
        site = self.make_site("machine-r")
        site.write_text(site.read_text().replace('public_url =', 'branch = "results"\npublic_url ='))
        self.site = site
        self.assertEqual(self.run_version(), 0)
        reader = self.tmp / "results"
        subprocess.run(["git", "clone", "--quiet", "-b", "results", str(self.data), str(reader)],
                       check=True)
        self.assertTrue((reader / "data" / "machine-r.csv").exists())
        self.assertFalse((reader / "code.txt").exists())
        code = subprocess.run(["git", "-C", str(self.data), "rev-list", "--count", "code"],
                              capture_output=True, text=True, check=True)
        self.assertEqual(code.stdout.strip(), "1")

    def test_changing_the_target_repo_publishes_to_the_new_one(self):
        self.finished_run("COMPLETED", ALL_PASS)
        other = self.tmp / "other.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(other)], check=True)
        self.site.write_text(self.site.read_text().replace(f'repo = "{self.data}"',
                                                           f'repo = "{other}"'))
        self.assertEqual(self.tick(), 0)
        reader = self.tmp / "other"
        subprocess.run(["git", "clone", "--quiet", str(other), str(reader)], check=True)
        self.assertTrue((reader / "data" / "machine-a.csv").exists())


class RunGroups(unittest.TestCase):
    """The runner inside the job, with a fake launcher that obeys each test file's name."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "dragon").write_text("""#!/bin/bash
for arg in "$@"; do
    case "$arg" in --junitxml=*) junit="${arg#--junitxml=}";; *test_*.py) file="$arg";; esac
done
name=$(basename "$file" .py)
echo "$name ran with $*"
case "$name" in
    test_hang) exec sleep 60;;
    test_crash) exit 0;;
    test_partial)
        cat >> "$HPC_MONITOR_LIVE_RESULTS" <<'LIVE'
{"test": "f.py::test_good", "outcome": null, "message": ""}
{"test": "f.py::test_good", "outcome": "passed", "message": ""}
{"test": "f.py::test_gpu", "outcome": null, "message": ""}
{"test": "f.py::test_gpu", "outcome": "skipped", "message": "no GPU"}
{"test": "f.py::test_bad", "outcome": null, "message": ""}
{"test": "f.py::test_bad", "outcome": "failed", "message": "Timeout after 120.0s\\u0001"}
{"test": "f.py::test_stuck", "outcome": null, "message": ""}
{"test": "f.py::test_cut
LIVE
        exec sleep 60;;
    test_empty) echo '<testsuites><testsuite/></testsuites>' > "$junit"; exit 5;;
    *) echo "<testsuites><testsuite><testcase classname='$name' name='ok'/></testsuite></testsuites>" > "$junit";;
esac
""")
        (self.bin / "dragon").chmod(0o755)
        (self.bin / "dragon-cleanup").write_text("#!/bin/bash\nexit 0\n")
        (self.bin / "dragon-cleanup").chmod(0o755)
        self.suite = self.tmp / "suite"
        self.suite.mkdir()

    def run_groups(self, files, *args):
        for name in files:
            (self.suite / name).write_text("")
        junit = self.tmp / "run" / "junit.xml"
        junit.parent.mkdir(exist_ok=True)
        env = os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}"}
        env.pop("SLURM_JOB_ID", None)
        env.pop("SLURM_NNODES", None)
        result = subprocess.run(
            [sys.executable, str(REPO / "jobs" / "run_groups.py"), "--suite", str(self.suite),
             "--junit", str(junit), *args, "--", "-w", "slurm"],
            capture_output=True, text=True, env=env, check=False, timeout=90,
        )
        return result, junit

    def names(self, junit):
        return [f"{c.get('classname')}.{c.get('name')}"
                for c in ET.parse(junit).getroot().iter("testcase")]

    def test_results_from_every_file_are_merged(self):
        result, junit = self.run_groups(["test_a.py", "test_b.py"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.names(junit), ["test_a.ok", "test_b.ok"])

    def test_hang_costs_only_that_file(self):
        _, junit = self.run_groups(["test_a.py", "test_hang.py", "test_z.py"], "--budget", "2")
        self.assertEqual(self.names(junit), ["test_a.ok", "test_hang.launcher", "test_z.ok"])
        tests, _ = hpc_monitor.parse_junit(junit)
        self.assertEqual(sum(t["status"] == "failed" for t in tests), 1)
        self.assertIn("exceeded its budget", tests[1]["output"])

    def test_stopped_run_keeps_finished_tests_and_their_errors(self):
        _, junit = self.run_groups(["test_partial.py"], "--budget", "2")
        self.assertEqual(self.names(junit), [
            "test_partial.test_good", "test_partial.test_gpu", "test_partial.test_bad",
            "test_partial.launcher"])
        tests, skipped = hpc_monitor.parse_junit(junit)
        self.assertEqual(skipped, ["test_partial.test_gpu"])
        by_name = {t["name"]: t for t in tests}
        self.assertEqual(by_name["test_partial.test_good"]["status"], "passed")
        self.assertIn("Timeout after 120.0s", by_name["test_partial.test_bad"]["output"])
        self.assertIn("test_stuck was still running", by_name["test_partial.launcher"]["output"])

    def test_live_plugin_records_results_as_they_happen(self):
        sys.path.insert(0, str(REPO / "jobs"))
        self.addCleanup(sys.path.remove, str(REPO / "jobs"))
        import pytest_live

        live = self.tmp / "live.jsonl"
        os.environ["HPC_MONITOR_LIVE_RESULTS"] = str(live)
        self.addCleanup(os.environ.pop, "HPC_MONITOR_LIVE_RESULTS")

        def report(when, outcome, longrepr=None):
            return types.SimpleNamespace(nodeid="f.py::t", when=when, outcome=outcome,
                                         longrepr=longrepr)

        crash = types.SimpleNamespace(reprcrash=types.SimpleNamespace(message="boom"))
        pytest_live.pytest_runtest_logstart("f.py::t", None)
        pytest_live.pytest_runtest_logreport(report("setup", "passed"))
        pytest_live.pytest_runtest_logreport(report("call", "failed", crash))
        pytest_live.pytest_runtest_logreport(report("teardown", "passed"))
        records = [json.loads(line) for line in live.read_text().splitlines()]
        self.assertEqual([(r["outcome"], r["message"]) for r in records],
                         [(None, ""), ("failed", "boom")])

    def test_crash_without_results_is_a_failure(self):
        _, junit = self.run_groups(["test_crash.py"])
        self.assertEqual(self.names(junit), ["test_crash.launcher"])

    def test_file_without_tests_is_not_a_failure(self):
        _, junit = self.run_groups(["test_a.py", "test_empty.py"])
        self.assertEqual(self.names(junit), ["test_a.ok"])

    def test_last_files_run_last_in_the_order_given(self):
        _, junit = self.run_groups(["test_a.py", "test_mpi.py", "test_scale.py", "test_z.py"],
                                   "--last", "test_scale.py,test_mpi.py")
        self.assertEqual(self.names(junit), ["test_a.ok", "test_z.ok", "test_scale.ok", "test_mpi.ok"])

    def test_used_up_time_marks_remaining_files_as_not_run(self):
        _, junit = self.run_groups(["test_a.py", "test_b.py"], "--total", "30")
        tests, _ = hpc_monitor.parse_junit(junit)
        self.assertEqual([t["name"] for t in tests], ["test_a.launcher", "test_b.launcher"])
        self.assertIn("did not run", tests[0]["output"])

    def test_launcher_arguments_are_passed_and_suite_is_importable(self):
        self.run_groups(["test_a.py"])
        log = (self.tmp / "run" / "groups" / "test_a.log").read_text()
        self.assertIn("ran with -w slurm", log)

    def test_empty_suite_exits_non_zero(self):
        result, junit = self.run_groups([])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(junit.exists())


if __name__ == "__main__":
    unittest.main()
