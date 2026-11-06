"""Offline tests: fake SLURM and pip on PATH, and a local server standing in for CDash."""

import contextlib
import http.server
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
import xml.etree.ElementTree as ET

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import hpc_monitor  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "pytest_mixed.xml"
ALL_PASS = """<?xml version="1.0"?><testsuites><testsuite tests="2">
<testcase classname="t" name="a" time="1.5"/><testcase classname="t" name="b" time="0.5"/>
</testsuite></testsuites>"""
ONLY_SKIPS = """<?xml version="1.0"?><testsuites><testsuite tests="1">
<testcase classname="t" name="a"><skipped message="no gpu"/></testcase></testsuite></testsuites>"""


class FakeCDash(http.server.BaseHTTPRequestHandler):
    """Same replacement rule as the real server: only a row marked done gets replaced."""

    def log_message(self, *args):
        pass

    def do_PUT(self):
        server = self.server
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        server.uploads.append((query["FileName"], query, body, self.headers.get("Authorization")))
        if server.fail_uploads > 0:
            server.fail_uploads -= 1
            self.send_error(500)
            return
        key = (query["site"], query["build"], query["stamp"])
        row = server.rows.setdefault(key, {"done": False, "tests": []})
        if query["FileName"] == "Done.xml":
            if server.ignore_done <= 0:
                row["done"] = True
            server.ignore_done -= 1
        elif not server.freeze and query["stamp"] != server.freeze_stamp:
            if row["done"]:
                row.update(done=False, tests=[])
            if query["FileName"] == "Test.xml":
                row["tests"] += [t.get("Status") for t in ET.fromstring(body).iter("Test")]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<cdash><status>OK</status></cdash>")

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        builds = []
        for (site, name, stamp), row in self.server.rows.items():
            tests = row["tests"]
            count = (lambda s: tests.count(s) if tests else -1)
            builds.append({"node": {
                "name": name, "stamp": stamp, "site": {"name": site},
                "passedTestsCount": count("passed"), "failedTestsCount": count("failed"),
                "notRunTestsCount": count("notrun"),
            }})
        data = {"data": {"projects": {"edges": [{"node": {"builds": {"edges": builds}}}]}}}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"info": {"version": self.server.latest}}).encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCDash)
        self.server.rows, self.server.uploads = {}, []
        self.server.fail_uploads = self.server.ignore_done = 0
        self.server.freeze = False
        self.server.freeze_stamp = None
        self.server.latest = "0.6.0"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

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
            cat "$FAKE/sacct.txt" 2>/dev/null
            exit 0
        """)

        self.venv = self.tmp / "venv"
        (self.venv / "bin").mkdir(parents=True)
        python = self.venv / "bin" / "python"
        python.write_text(f"""#!/bin/bash
FAKE={self.fake}
if [ "$1" = "-c" ]; then
    file="$FAKE/installed/$3"
    [ -e "$file" ] && cat "$file" || exit 1
    exit 0
fi
echo "$@" >> "$FAKE/pip.log"
[ -e "$FAKE/pip.fail" ] && {{ echo "pip: no matching distribution" >&2; exit 1; }}
[ -e "$FAKE/pip.wrong" ] && exit 0
shift 4
for spec in "$@"; do
    case "$spec" in /*) cat "$FAKE/source_version" > "$FAKE/installed/rhapsody-py"; continue;; esac
    name="${{spec%%[*}}"; name="${{name%%=*}}"
    version="${{spec##*==}}"; [ "$version" = "$spec" ] && version=9.9
    echo "$version" > "$FAKE/installed/$name"
done
echo 0.15.0 > "$FAKE/installed/dragonhpc"
""")
        python.chmod(0o755)
        self.install("rhapsody-py", "0.6.0")
        self.install("pytest", "9.2")
        self.install("pytest-asyncio", "1.0")
        self.install("dragonhpc", "0.15.0")

        self.source = self.tmp / "source_repo"
        self.git("init", "--quiet", str(self.source))
        (self.source / "suite.txt").write_text("suite")
        for version in ("0.6.0", "0.7.0"):
            self.git("-C", str(self.source), "add", ".")
            self.git("-C", str(self.source), "commit", "--quiet", "--allow-empty", "-m", version)
            self.git("-C", str(self.source), "tag", f"v{version}")

        self.env = unittest.mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake),
            "HPC_MONITOR_HOME": str(self.tmp / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        self.clock = [1_800_000_000.0]
        patcher = unittest.mock.patch.object(hpc_monitor, "now", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

        self.site = self.make_site("machine-a")

    def git(self, *args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
            check=True, capture_output=True,
        )

    def script(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def install(self, package, version):
        (self.fake / "installed" / package).write_text(version + "\n")

    def make_site(self, machine, runs=("one",), archive=""):
        directory = self.tmp / "sites" / machine
        directory.mkdir(parents=True, exist_ok=True)
        entries = ""
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        for name in runs:
            (directory / f"{name}.sbatch").write_text(f"#!/bin/bash\n# {name}\n")
            entries += f"""
[[run]]
name = "{name}"
package = "rhapsody-py"
job_script = "{name}.sbatch"
source_repo = "file://{self.source}"
pypi_url = "{url}/pypi"
"""
        path = directory / "site.toml"
        path.write_text(f"""
machine = "{machine}"
venv = "{self.venv}"

[cdash]
project = "P"
url = "{url}"

[timing]
readback_seconds = 0
{archive}
{entries}""")
        return path

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return hpc_monitor.main(["tick", "--site", str(self.site)])

    def run_version(self, version="0.6.0", site=None, extra=()):
        with contextlib.redirect_stdout(io.StringIO()):
            return hpc_monitor.main(
                ["run", "--site", str(site or self.site), "--version", version, *extra])

    def state(self, machine="machine-a"):
        return json.loads((self.tmp / "state" / machine / "state.json").read_text())

    def run_record(self, index=-1):
        return self.state()["runs"][index]

    def set_job(self, state, exit_code="0:0", reason="None"):
        (self.fake / "sacct.txt").write_text(
            f"4242|{state}|{exit_code}|00:10:00|2027-01-15T08:00:00|2027-01-15T08:10:00|n1|{reason}\n")

    def write_junit(self, text):
        run_dir = pathlib.Path(self.run_record()["run_dir"])
        (run_dir / "junit.xml").write_text(text)

    def row(self, index=0):
        return list(self.server.rows.values())[index]

    def files(self):
        return [upload[0] for upload in self.server.uploads]

    def later(self, seconds):
        self.clock[0] += seconds


class TestVerdict(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "junit.xml"

    def decide(self, state, junit=None, reason=""):
        if junit is not None:
            self.path.write_text(junit)
        return hpc_monitor.decide({"state": state, "reason": reason}, self.path)

    def test_completed_all_pass_is_green(self):
        verdict = self.decide("COMPLETED", ALL_PASS)
        self.assertTrue(verdict["ok"])
        self.assertTrue(verdict["job_ok"])

    def test_test_failures_are_red(self):
        verdict = hpc_monitor.decide({"state": "COMPLETED"}, FIXTURE)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "2 of 3 tests failed")

    def test_pytest_nonzero_exit_counts_as_test_failure(self):
        verdict = hpc_monitor.decide({"state": "FAILED"}, FIXTURE)
        self.assertEqual(verdict["reason"], "2 of 3 tests failed")
        self.assertTrue(verdict["job_ok"])

    def test_completed_without_junit_is_red(self):
        verdict = self.decide("COMPLETED")
        self.assertFalse(verdict["ok"])
        self.assertIn("missing", verdict["reason"])

    def test_corrupt_junit_is_red(self):
        verdict = self.decide("COMPLETED", "<testsuites><not closed")
        self.assertFalse(verdict["ok"])
        self.assertIn("corrupt", verdict["reason"])

    def test_every_test_skipped_is_red(self):
        verdict = self.decide("COMPLETED", ONLY_SKIPS)
        self.assertFalse(verdict["ok"])
        self.assertIn("no tests executed", verdict["reason"])

    def test_slurm_killed_states_are_red_with_reason(self):
        for state in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "CANCELLED", "NODE_FAIL",
                      "BOOT_FAIL", "DEADLINE", "PREEMPTED"):
            with self.subTest(state=state):
                verdict = self.decide(state, reason="Resources")
                self.assertFalse(verdict["ok"])
                self.assertFalse(verdict["job_ok"])
                self.assertIn(state, verdict["reason"])
                self.assertIn("Resources", verdict["reason"])

    def test_killed_job_keeps_partial_results_but_stays_red(self):
        verdict = self.decide("TIMEOUT", ALL_PASS)
        self.assertFalse(verdict["ok"])
        self.assertEqual(len(verdict["tests"]), 2)

    def test_unknown_job_is_red(self):
        verdict = hpc_monitor.decide(None, self.path)
        self.assertFalse(verdict["ok"])
        self.assertIn("no longer known", verdict["reason"])


class TestConversion(unittest.TestCase):
    def test_real_pytest_output(self):
        tests, skipped = hpc_monitor.parse_junit(FIXTURE)
        self.assertEqual({t["name"]: t["status"] for t in tests},
                         {"test_s.test_good": "passed", "test_s.test_broken": "failed",
                          "test_s.test_err": "failed"})
        self.assertEqual(skipped, ["test_s.test_skip"])

    def test_error_text_is_kept(self):
        tests, _ = hpc_monitor.parse_junit(FIXTURE)
        error = next(t for t in tests if t["name"] == "test_s.test_err")
        self.assertIn("fixture boom", error["output"])

    def test_documents_escape_special_characters(self):
        tests = [{"name": "a<b>", "status": "failed", "output": "x & <y>", "seconds": 1.0}]
        body = hpc_monitor.test_xml("m&m", 'b"1', "20270101-0000-Experimental", 0, 60, tests, "1")
        root = ET.fromstring(body)
        self.assertEqual(root.get("Name"), "m&m")
        self.assertEqual(root.find(".//Test/Name").text, "a<b>")


class TestScrub(unittest.TestCase):
    def scrub(self, text):
        return hpc_monitor.scrub(text, user="alice", home="/home/alice")

    def test_home_and_username(self):
        result = self.scrub("/home/alice/run failed for alice, not malice")
        self.assertEqual(result, "~/run failed for <user>, not malice")

    def test_secrets(self):
        for text in ("API_KEY=abc123", "CDASH_TOKEN: abc123", "password=abc123",
                     "Authorization: Bearer abc123"):
            with self.subTest(text=text):
                self.assertNotIn("abc123", self.scrub(text))

    def test_control_characters_and_ansi_removed(self):
        result = self.scrub("a\x00b\x1b[31mred\x1b[0m\x07 c\td\n")
        self.assertEqual(result, "abred c\td\n")
        ET.fromstring(f"<x>{result}</x>")

    def test_trim_keeps_the_end(self):
        result = hpc_monitor.trim("a" * 100 + "END", limit=10)
        self.assertTrue(result.endswith("END"))
        self.assertLess(len(result), 40)


class TestStages(Base):
    def test_full_sequence_pending_running_final(self):
        self.assertEqual(self.run_version(), 0)
        self.assertEqual(self.run_record()["stage"], "pending_posted")
        self.assertEqual(self.files(), ["Configure.xml", "Update.xml"])
        self.assertEqual(self.row()["tests"], [])

        self.set_job("PENDING")
        self.tick()
        self.assertEqual(len(self.files()), 2, "no upload while the job is queued")

        self.set_job("RUNNING")
        self.tick()
        self.assertEqual(self.row()["tests"], ["notrun"])
        self.assertEqual(self.files()[-2:], ["Test.xml", "Done.xml"])
        self.assertEqual(self.run_record()["stage"], "running_done")

        self.later(900)
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.tick()
        self.assertEqual(self.row()["tests"], ["passed", "passed", "passed"])
        self.assertEqual(self.files()[-3:], ["Update.xml", "Test.xml", "Done.xml"])
        self.assertEqual(self.run_record()["stage"], "archived")
        self.assertEqual(len(self.server.rows), 1)

    def test_job_finishing_between_ticks_skips_running(self):
        self.run_version()
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.tick()
        self.assertEqual(self.files(), ["Configure.xml", "Update.xml", "Test.xml", "Done.xml"])
        self.assertEqual(self.row()["tests"], ["passed", "passed", "passed"])
        self.assertEqual(self.run_record()["stage"], "archived")

    def test_replacement_waits_for_the_gap(self):
        self.run_version()
        self.set_job("RUNNING")
        self.tick()
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.later(60)
        count = len(self.files())
        self.tick()
        self.assertEqual(len(self.files()), count, "too soon after Done.xml")
        self.later(600)
        self.tick()
        self.assertEqual(self.run_record()["stage"], "archived")

    def test_one_dashboard_change_per_tick(self):
        self.run_version()
        self.assertEqual(self.run_record()["stage"], "pending_posted",
                         "submitting must not also post running in the same call")

    def test_failed_run_uploads_test_output_and_skips(self):
        self.run_version()
        self.write_junit(FIXTURE.read_text())
        self.set_job("FAILED", "1:0")
        self.tick()
        self.assertEqual(sorted(self.row()["tests"]), ["failed", "failed", "passed", "passed"])
        body = [u[2] for u in self.server.uploads if u[0] == "Test.xml"][-1]
        self.assertIn("fixture boom", body)
        self.assertIn("test_s.test_skip", body)
        self.assertNotIn('Status="notrun"', body, "skips must not look pending")

    def test_status_test_carries_identity(self):
        self.run_version()
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.tick()
        body = [u[2] for u in self.server.uploads if u[0] == "Test.xml"][-1]
        for expected in ("SLURM job ID: 4242", "Version: 0.6.0", "dragonhpc: 0.15.0",
                         "Machine: machine-a", "PASSED"):
            self.assertIn(expected, body)
        start = ET.fromstring(body).find(".//StartTestTime").text
        self.assertNotEqual(start, str(int(self.clock[0])), "start comes from sacct")

    def test_timeout_shows_red_with_reason(self):
        self.run_version()
        self.set_job("TIMEOUT", "0:0")
        self.tick()
        self.assertEqual(self.row()["tests"], ["failed"])
        self.assertIn("TIMEOUT", self.run_record()["verdict"]["reason"])

    def test_cancelled_by_user_is_parsed(self):
        self.run_version()
        self.set_job("CANCELLED by 1000", "0:15")
        self.tick()
        self.assertEqual(self.row()["tests"], ["failed"])
        self.assertIn("CANCELLED", self.run_record()["verdict"]["reason"])

    def test_clean_exit_without_junit_is_red_with_message(self):
        self.run_version()
        self.set_job("COMPLETED")
        self.tick()
        self.assertEqual(self.row()["tests"], ["failed"])
        self.assertIn("JUnit file is missing", self.run_record()["verdict"]["reason"])


class TestFailures(Base):
    def test_upload_failure_retries_next_tick(self):
        self.run_version()
        self.set_job("RUNNING")
        self.server.fail_uploads = 1
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.run_record()["stage"], "pending_posted")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.run_record()["stage"], "running_done")
        self.assertEqual(self.row()["tests"], ["notrun"])

    def test_sacct_down_gives_no_verdict(self):
        self.run_version()
        (self.fake / "sacct.fail").touch()
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.run_record()["stage"], "pending_posted")
        (self.fake / "sacct.fail").unlink()
        self.set_job("RUNNING")
        self.assertEqual(self.tick(), 0)

    def test_missing_job_waits_for_grace_period_then_red(self):
        self.run_version()
        self.tick()
        self.assertEqual(self.run_record()["stage"], "pending_posted")
        self.later(3700)
        self.tick()
        self.assertEqual(self.row()["tests"], ["failed"])
        self.assertIn("no longer known", self.run_record()["verdict"]["reason"])

    def test_killed_after_sbatch_does_not_submit_twice(self):
        with unittest.mock.patch.object(hpc_monitor.Monitor, "stage_submitted",
                                        side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.run_version()
        (self.fake / "sacct.name").write_text("4242\n")
        record = self.state()["runs"][-1]
        self.assertEqual(record["stage"], "submitted")
        self.tick()
        self.assertEqual((self.fake / "sbatch.log").read_text().count("--parsable"), 1)

    def test_killed_right_after_sbatch_recovers_the_job(self):
        real_submit = hpc_monitor.Monitor.submit_job

        def submit_then_die(monitor, spec, run):
            real_submit(monitor, spec, run)
            raise KeyboardInterrupt

        with unittest.mock.patch.object(hpc_monitor.Monitor, "submit_job", submit_then_die), \
                self.assertRaises(KeyboardInterrupt):
            self.run_version()
        self.assertEqual(self.run_record()["stage"], "submitting")
        (self.fake / "sacct.name").write_text("4242\n")
        self.tick()
        self.assertEqual((self.fake / "sbatch.log").read_text().count("--parsable"), 1)
        self.assertEqual(self.run_record()["job_id"], "4242")
        self.assertEqual(self.run_record()["stage"], "pending_posted")

    def test_killed_before_sbatch_submits_on_next_tick(self):
        with unittest.mock.patch.object(hpc_monitor.Monitor, "submit_job",
                                        side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.run_version()
        self.tick()
        self.assertEqual((self.fake / "sbatch.log").read_text().count("--parsable"), 1)
        self.assertEqual(self.run_record()["stage"], "pending_posted")

    def test_new_run_refused_while_one_is_open(self):
        self.run_version()
        self.assertEqual(self.run_version(), 1)
        self.assertEqual((self.fake / "sbatch.log").read_text().count("--parsable"), 1)
        self.assertEqual(len(self.state()["runs"]), 1)

    def test_second_tick_exits_while_lock_is_held(self):
        import fcntl
        (self.tmp / "state" / "machine-a").mkdir(parents=True)
        with open(self.tmp / "state" / "machine-a" / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.tick(), 0)
        self.assertFalse((self.tmp / "state/machine-a/state.json").exists())

    def test_corrected_run_after_close(self):
        self.run_version()
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.tick()
        self.later(120)
        self.assertEqual(self.run_version(), 0)
        self.assertEqual(len(self.state()["runs"]), 2)
        self.assertEqual(len(self.server.rows), 2, "a rerun gets a new row")


class TestRepair(Base):
    def running_row_broken_by_lost_done(self):
        self.run_version()
        self.set_job("RUNNING")
        self.server.ignore_done = 1
        self.tick()
        self.assertEqual(self.run_record()["stage"], "running_done")
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.later(900)
        self.tick()  # Update.xml doesn't replace, because Done was never processed.
        self.assertEqual(self.run_record()["stage"], "final_update_posted")

    def test_wrong_row_is_left_alone_until_it_has_had_time(self):
        self.running_row_broken_by_lost_done()
        count = len(self.files())
        self.later(600)
        self.tick()
        self.assertEqual(len(self.files()), count)

    def test_wrong_row_is_repaired(self):
        self.running_row_broken_by_lost_done()
        self.later(1800)
        self.tick()
        self.assertEqual(self.run_record()["stage"], "repair_wait")
        self.assertEqual(self.files()[-1], "Done.xml")
        self.later(900)
        self.tick()
        self.assertEqual(self.row()["tests"], ["passed", "passed", "passed"])
        self.assertEqual(self.run_record()["stage"], "archived")
        self.assertEqual(len(self.server.rows), 1)

    def test_repair_of_running_row_goes_straight_to_final_if_finished(self):
        self.run_version()
        self.set_job("RUNNING")
        self.server.freeze = True
        self.tick()
        self.assertEqual(self.run_record()["stage"], "running_posted")
        self.later(1800)
        self.tick()
        self.assertEqual(self.run_record()["repair_kind"], "running")
        self.server.freeze = False
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.later(900)
        self.tick()
        self.assertEqual(self.row()["tests"], ["passed", "passed", "passed"])
        self.assertEqual(self.run_record()["stage"], "archived")

    def test_new_stamp_fallback_publishes_the_result(self):
        self.running_row_broken_by_lost_done()
        original = self.run_record()["stamp"]
        self.server.freeze_stamp = original  # the old row never recovers, a new one works
        for _ in range(20):
            self.later(1800)
            self.tick()
            if self.run_record()["stage"] == "archived":
                break
        record = self.run_record()
        self.assertEqual(record["stage"], "archived")
        self.assertTrue(record["fallback"])
        self.assertFalse(record.get("unconfirmed"))
        self.assertNotEqual(record["stamp"], original)
        new_row = self.server.rows[("machine-a", "one", record["stamp"])]
        self.assertEqual(new_row["tests"], ["passed", "passed", "passed"])

    def test_unconfirmed_result_is_logged_and_closed(self):
        self.running_row_broken_by_lost_done()
        self.server.freeze = True
        for _ in range(20):
            self.later(1800)
            self.tick()
            if self.run_record()["stage"] == "archived":
                break
        self.assertTrue(self.run_record().get("unconfirmed"))
        self.assertEqual(self.run_record()["stage"], "archived")


class TestEnvironment(Base):
    def test_upgrade_happens_when_version_differs(self):
        self.assertEqual(self.run_version("0.7.0"), 0)
        self.assertIn("rhapsody-py[dragon]==0.7.0", (self.fake / "pip.log").read_text())
        self.assertEqual(self.run_record()["versions"]["dragonhpc"], "0.15.0")

    def source_site(self, extra=""):
        (self.fake / "source_version").write_text("0.6.0\n")
        site = self.make_site("machine-a")
        text = site.read_text().replace(
            'package = "rhapsody-py"\n',
            'package = "rhapsody-py"\n'
            'source_tag = "v0.7.0"\n'
            'install = "{source_dir}[dragon]"\n' + extra,
        )
        site.write_text(text)
        return site

    def test_source_install_always_runs_from_the_clone(self):
        self.run_version("0.6.0", site=self.source_site())
        source_dir = self.run_record()["source_dir"]
        self.assertIn(f"{source_dir}[dragon]", (self.fake / "pip.log").read_text())
        self.assertEqual(self.run_record()["stage"], "pending_posted")

    def test_source_commit_is_recorded_and_reported(self):
        self.run_version("0.6.0", site=self.source_site())
        commit = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "--short=12", "v0.7.0"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(self.run_record()["versions"]["source commit"], commit)

    def test_source_version_mismatch_gives_red_row(self):
        site = self.source_site()
        (self.fake / "source_version").write_text("0.7.0\n")
        self.run_version("0.6.0", site=site)
        self.assertFalse((self.fake / "sbatch.log").exists())
        self.assertIn("wanted 0.6.0", self.run_record()["verdict"]["reason"])

    def test_required_version_mismatch_gives_red_row(self):
        site = self.source_site('require_versions = { dragonhpc = "0.14.1" }\n')
        self.run_version("0.6.0", site=site)
        self.assertFalse((self.fake / "sbatch.log").exists())
        self.assertIn("required 0.14.1", self.run_record()["verdict"]["reason"])
        self.assertEqual(self.row()["tests"], ["failed"])

    def test_required_version_match_submits(self):
        site = self.source_site('require_versions = { dragonhpc = "0.15.0" }\n')
        self.run_version("0.6.0", site=site)
        self.assertTrue((self.fake / "sbatch.log").exists())

    def test_no_upgrade_when_version_matches(self):
        self.run_version("0.6.0")
        self.assertFalse((self.fake / "pip.log").exists())

    def test_missing_test_packages_are_installed(self):
        (self.fake / "installed" / "pytest-asyncio").unlink()
        self.run_version("0.6.0")
        pip_log = (self.fake / "pip.log").read_text()
        self.assertIn("pytest-asyncio", pip_log)
        self.assertNotIn("rhapsody-py", pip_log, "a matching version must not be reinstalled")
        self.assertNotIn(" pytest ", pip_log, "installed packages must be left alone")

    def test_no_upgrade_with_a_job_open(self):
        self.run_version("0.6.0")
        self.assertEqual(self.run_version("0.7.0"), 1)
        self.assertFalse((self.fake / "pip.log").exists())

    def test_failed_upgrade_gives_red_row_and_no_sbatch(self):
        (self.fake / "pip.fail").touch()
        self.run_version("0.7.0")
        self.assertFalse((self.fake / "sbatch.log").exists())
        self.assertEqual(self.row()["tests"], ["failed"])
        self.assertIn("could not start", self.run_record()["verdict"]["reason"])
        self.assertEqual(self.run_record()["stage"], "archived")

    def test_version_mismatch_after_upgrade_gives_red_row(self):
        (self.fake / "pip.wrong").touch()
        self.run_version("0.7.0")
        self.assertFalse((self.fake / "sbatch.log").exists())
        self.assertIn("wanted 0.7.0", self.run_record()["verdict"]["reason"])

    def test_missing_tag_gives_red_row(self):
        self.run_version("0.6.0")
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.tick()
        self.install("rhapsody-py", "0.8.0")
        self.later(120)
        self.run_version("0.8.0")
        self.assertEqual(self.run_record()["stage"], "archived")
        self.assertFalse(self.run_record()["verdict"]["ok"])

    def test_sbatch_failure_gives_red_row(self):
        (self.fake / "sbatch.fail").touch()
        self.run_version("0.6.0")
        self.assertIn("denied", self.run_record()["verdict"]["reason"])
        self.assertEqual(self.row()["tests"], ["failed"])

    def test_contract_variables_reach_the_job_script(self):
        self.run_version("0.6.0")
        env = (self.fake / "sbatch.env").read_text()
        for name in ("RUN_DIR", "VENV", "SOURCE_DIR", "VERSION", "REPO_DIR"):
            self.assertIn(f"HPC_MONITOR_{name}=", env)
        self.assertTrue(pathlib.Path(self.run_record()["source_dir"], "suite.txt").exists())


class TestReleases(Base):
    def test_first_sight_records_without_spending_allocation(self):
        self.tick()
        self.assertEqual(self.state()["seen"], {"one": "0.6.0"})
        self.assertFalse((self.fake / "sbatch.log").exists())

    def test_new_release_starts_a_run_once(self):
        self.tick()
        self.server.latest = "0.7.0"
        self.tick()
        self.assertFalse((self.fake / "sbatch.log").exists(), "checked at most once a day")
        self.later(86401)
        self.tick()
        self.assertEqual(self.run_record()["version"], "0.7.0")
        self.later(86401)
        self.tick()
        self.assertEqual((self.fake / "sbatch.log").read_text().count("--parsable"), 1)

    def test_no_release_check_while_a_run_is_open(self):
        self.run_version("0.6.0")
        self.set_job("PENDING")
        self.server.latest = "0.7.0"
        self.later(86401)
        self.tick()
        self.assertEqual(len(self.state()["runs"]), 1)

    def test_manual_run_counts_as_seen(self):
        self.run_version("0.6.0")
        self.assertEqual(self.state()["seen"], {"one": "0.6.0"})


class TestDryRun(Base):
    def test_run_changes_nothing(self):
        (self.fake / "installed" / "pytest").unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = hpc_monitor.main(["run", "--site", str(self.site), "--version", "0.7.0",
                                     "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("would run: pip install", output.getvalue())
        self.assertIn("would submit", output.getvalue())
        self.assertFalse((self.tmp / "state").exists())
        self.assertFalse((self.fake / "pip.log").exists())
        self.assertFalse((self.fake / "sbatch.log").exists())
        self.assertEqual(self.server.uploads, [])

    def test_tick_on_open_run_changes_nothing(self):
        self.run_version()
        before = self.state()
        count = len(self.files())
        self.set_job("RUNNING")
        with contextlib.redirect_stdout(io.StringIO()):
            hpc_monitor.main(["tick", "--site", str(self.site), "--dry-run"])
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.files()), count)


class TestExpandability(Base):
    def test_two_runs_with_different_scripts_share_one_path(self):
        site = self.make_site("machine-b", runs=("cpu-suite", "gpu-suite"))
        code = hpc_monitor.main(["run", "--site", str(site), "--version", "0.6.0",
                                 "--run", "gpu-suite"])
        self.assertEqual(code, 0)
        self.assertIn("gpu-suite.sbatch", (self.fake / "sbatch.log").read_text())
        key = next(iter(self.server.rows))
        self.assertEqual(key[:2], ("machine-b", "gpu-suite"))

    def test_runs_go_one_after_another(self):
        site = self.make_site("machine-b", runs=("cpu-suite", "gpu-suite"))
        hpc_monitor.main(["run", "--site", str(site), "--version", "0.6.0", "--run", "cpu-suite"])
        code = hpc_monitor.main(["run", "--site", str(site), "--version", "0.6.0",
                                 "--run", "gpu-suite"])
        self.assertEqual(code, 1)

    def test_no_machine_specifics_in_the_module(self):
        source = (REPO / "hpc_monitor.py").read_text().lower()
        for machine in ("stellar", "perlmutter", "bridges", "delta", "--account", "--partition",
                        "--qos", "gpus-per-node"):
            self.assertNotIn(machine, source)
        self.assertIsNone(re.search(r"#\s*sbatch", source))

    def test_site_files_load(self):
        for path in (REPO / "sites").glob("*/site.toml"):
            with self.subTest(path=path):
                config = hpc_monitor.load_config(path)
                for run in config["run"]:
                    self.assertTrue((path.parent / run["job_script"]).exists())


class TestToken(Base):
    def test_token_is_sent_when_present(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "cdash.token").write_text("secret\n")
        self.run_version()
        self.assertEqual(self.server.uploads[0][3], "Bearer secret")

    def test_no_token_for_open_projects(self):
        self.run_version()
        self.assertIsNone(self.server.uploads[0][3])


class TestArchive(Base):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "archive.git"
        self.git("init", "--quiet", "--bare", str(self.remote))
        seed = self.tmp / "seed"
        self.git("clone", "--quiet", str(self.remote), str(seed))
        self.git("-C", str(seed), "commit", "--quiet", "--allow-empty", "-m", "start")
        self.git("-C", str(seed), "push", "--quiet", "origin", "HEAD:main")
        self.site = self.make_site("machine-a", archive=f"""
[archive]
enabled = true
repo = "file://{self.remote}"
branch = "results"
""")

    def show(self, path):
        return subprocess.run(
            ["git", "-C", str(self.remote), "show", f"results:{path}"],
            capture_output=True, text=True, check=True,
        ).stdout

    def finish_run(self):
        self.run_version()
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        return self.tick()

    def test_results_are_pushed(self):
        self.assertEqual(self.finish_run(), 0)
        prefix = "results/machine-a/one/0.6.0/4242"
        summary = json.loads(self.show(f"{prefix}/summary.json"))
        self.assertEqual(summary["job_id"], "4242")
        self.assertTrue(summary["verdict"]["ok"])
        self.assertIn("testcase", self.show(f"{prefix}/junit.xml"))
        self.assertEqual(self.run_record()["stage"], "archived")

    def test_archive_failure_retries_without_touching_cdash(self):
        (self.remote / "hooks" / "pre-receive").write_text("#!/bin/bash\nexit 1\n")
        (self.remote / "hooks" / "pre-receive").chmod(0o755)
        self.assertEqual(self.finish_run(), 1)
        self.assertEqual(self.run_record()["stage"], "final_done")
        count = len(self.files())
        (self.remote / "hooks" / "pre-receive").unlink()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.run_record()["stage"], "archived")
        self.assertEqual(len(self.files()), count)

    def test_second_run_adds_to_the_branch(self):
        self.finish_run()
        self.later(120)
        (self.fake / "sbatch.log").unlink()
        self.script("sbatch", "echo 4343")
        self.run_version()
        self.write_junit(ALL_PASS)
        self.set_job("COMPLETED")
        self.tick()
        self.assertIn("testcase", self.show("results/machine-a/one/0.6.0/4242/junit.xml"))
        self.assertIn("testcase", self.show("results/machine-a/one/0.6.0/4343/junit.xml"))


if __name__ == "__main__":
    unittest.main()
