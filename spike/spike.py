#!/usr/bin/env python3
"""Throwaway CDash behaviour checks. See spike/RESULTS.md for what each check decides.

Submits synthetic spike-* builds to a SANDBOX project, then reports what CDash did:

    python3 spike/spike.py --project RADICAL-HPC-Test-Suite-Sandbox
    python3 spike/spike.py --project X --dry-run --out /some/dir   # writes payloads, sends nothing

Builds can only be removed by a project admin, so the production project is refused.
"""

import argparse
import calendar
import hashlib
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

BASE = "https://my.cdash.org"
PRODUCTION_PROJECT = "RADICAL-HPC-Test-Suite"
SITE = "spike-stellar"

# Build stamps: <YYYYMMDD-HHMM>-<track>. The track becomes the dashboard group.
REPLACE_STAMP = "20261105-0100-Experimental"
NO_DONE_STAMP = "20261105-0200-Experimental"
JUNIT_STAMP = "20261105-0300-Experimental"
LARGE_STAMP = "20261105-0400-Experimental"
RELEASE_1_STAMP = "20261005-1200-Release"  # custom track check
RELEASE_2_STAMP = "20261104-1200-Release"

LARGE_OUTPUT_BYTES = 2 * 1024 * 1024
# A project with builds but none today, to see what the default landing page shows.
STALE_PROJECTS = ("DREAM3D", "NCEPLIBS-sigio")


def epoch(stamp):
    return calendar.timegm(time.strptime(stamp[:13], "%Y%m%d-%H%M"))


def test_xml(build, stamp, tests, job_id):
    """Build a CTest Test.xml. tests is a list of (name, status, output)."""
    start = epoch(stamp)
    items = "".join(
        f"""<Test Status="{status}"><Name>{escape(name)}</Name><Path>./dragon</Path>
<FullName>./dragon/{escape(name)}</FullName><FullCommandLine>spike</FullCommandLine>
<Results><NamedMeasurement type="numeric/double" name="Execution Time"><Value>1.5</Value></NamedMeasurement>
<NamedMeasurement type="text/string" name="SLURM Job ID"><Value>{job_id}</Value></NamedMeasurement>
<Measurement><Value>{escape(output)}</Value></Measurement></Results></Test>"""
        for name, status, output in tests
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Site BuildName="{build}" BuildStamp="{stamp}" Name="{SITE}" Generator="radical-spike">
<Testing><StartDateTime>{stamp}</StartDateTime><StartTestTime>{start}</StartTestTime>
<TestList/>{items}<EndDateTime>{stamp}</EndDateTime><EndTestTime>{start + 60}</EndTestTime>
<ElapsedMinutes>1</ElapsedMinutes></Testing></Site>"""


def update_xml(build, stamp, version):
    """Build an Update.xml whose Revision carries the software version."""
    start = epoch(stamp)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Update mode="Client" Generator="radical-spike"><Site>{SITE}</Site><BuildName>{build}</BuildName>
<BuildStamp>{stamp}</BuildStamp><StartDateTime>{stamp}</StartDateTime><StartTime>{start}</StartTime>
<UpdateCommand>none</UpdateCommand><UpdateType>GIT</UpdateType><EndDateTime>{stamp}</EndDateTime>
<EndTime>{start}</EndTime><ElapsedMinutes>0</ElapsedMinutes>
<UpdateReturnStatus></UpdateReturnStatus><Revision>{version}</Revision><PriorRevision>{version}</PriorRevision></Update>"""


def done_xml(stamp):
    return f"<Done><buildId>0</buildId><time>{epoch(stamp) + 60}</time></Done>"


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="spike-junit" errors="0" failures="1" skipped="0" tests="2" time="3.0" timestamp="2026-11-05T03:00:00" hostname="spike-stellar">
<testcase classname="test_mpi" name="test_good" time="1.0"/><testcase classname="test_mpi" name="test_broken" time="2.0"><failure message="boom">Traceback: boom</failure></testcase></testsuite></testsuites>"""


class Client:
    def __init__(self, project, dry_run_dir):
        self.project = project
        self.dry_run_dir = dry_run_dir
        self.sent = 0

    def put(self, filename, body, build, stamp):
        self.sent += 1
        if self.dry_run_dir:
            path = self.dry_run_dir / f"{self.sent:02d}-{build}-{filename}"
            path.write_text(body)
            print(f"  wrote {path.name} ({len(body)} bytes)")
            return
        data = body.encode()
        query = urllib.parse.urlencode({
            "project": self.project, "FileName": filename, "build": build, "site": SITE,
            "stamp": stamp, "MD5": hashlib.md5(data).hexdigest(),
        })
        request = urllib.request.Request(f"{BASE}/submit.php?{query}", data=data, method="PUT")
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode()
        ok = "<status>OK</status>" in text
        print(f"  PUT {filename} build={build} stamp={stamp} -> {response.status} ok={ok}")
        if not ok:
            print(text[:500])

    def wait(self, label, seconds):
        if self.dry_run_dir:
            return
        print(f"-- waiting {seconds}s for parsing ({label})", flush=True)
        time.sleep(seconds)

    def builds(self):
        query = f'''{{ projects(filters:{{eq:{{name:"{self.project}"}}}}) {{ edges {{ node {{ buildCount builds(first:50) {{ edges {{ node {{
          id name stamp startTime buildType passedTestsCount failedTestsCount notRunTestsCount site {{ name }}
          updateStep {{ revision }}
          tests(first:10) {{ edges {{ node {{ name status output testMeasurements {{ name value }} }} }} }}
        }} }} }} }} }} }} }}'''
        request = urllib.request.Request(
            f"{BASE}/graphql", data=json.dumps({"query": query}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)

    def index(self, project, date=None):
        params = {"project": project} | ({"date": date} if date else {})
        url = f"{BASE}/api/v1/index.php?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=120) as response:
            return json.load(response)


def print_builds(result):
    if "errors" in result:
        print("GraphQL errors:", result["errors"][:2])
        return
    edges = result["data"]["projects"]["edges"]
    if not edges:
        print("Project not visible anonymously; it must exist and be public.")
        return
    for edge in edges[0]["node"]["builds"]["edges"]:
        build = edge["node"]
        print(build["id"], build["name"], build["stamp"], build["buildType"], build["site"]["name"],
              "pass/fail/notrun", build["passedTestsCount"], build["failedTestsCount"],
              build["notRunTestsCount"], "update", build["updateStep"])
        for test_edge in build["tests"]["edges"]:
            test = test_edge["node"]
            output = test["output"] or ""
            measurements = [(m["name"], m["value"]) for m in test["testMeasurements"]]
            print(f"    {test['name']} {test['status']} output_len={len(output)} "
                  f"output={output[:40]!r} {measurements}")


def report(client):
    print("== Builds")
    print_builds(client.builds())

    print("== Index API: release comparison, group names, version display")
    for date in ("2026-10-05", "2026-11-04", "2026-11-05"):
        for group in client.index(client.project, date).get("buildgroups", []):
            for build in group.get("builds", []):
                print(date, "group", group["name"], build.get("buildname"),
                      "test", build.get("test"), "update", build.get("update"))

    print("== Landing page with no date, on projects with no recent builds")
    for project in STALE_PROJECTS:
        data = client.index(project)
        count = sum(len(group.get("builds", [])) for group in data.get("buildgroups", []))
        print(project, "date", data.get("date"), "builds shown", count)


def retest_replacement(client, gap_seconds):
    """Pending, running, final with long gaps, printing the build after each step."""
    build, stamp = "spike-replace-slow", "20261105-0500-Experimental"
    steps = [
        ("pending", [("slurm-job", "notrun", "PENDING job 601")]),
        ("running", [("slurm-job", "notrun", "RUNNING job 601")]),
        ("final", [("test_ok", "passed", "fine"), ("slurm-job", "passed", "COMPLETED")]),
    ]
    for label, tests in steps:
        print(f"== {label}")
        client.put("Test.xml", test_xml(build, stamp, tests, "601"), build, stamp)
        client.put("Done.xml", done_xml(stamp), build, stamp)
        client.wait(label, gap_seconds)
        result = client.builds()
        edges = result["data"]["projects"]["edges"][0]["node"]["builds"]["edges"]
        matching = [e for e in edges if e["node"]["name"] == build]
        print(f"rows for {build}: {len(matching)}")
        for edge in matching:
            node = edge["node"]
            print(" ", node["id"], "pass/fail/notrun", node["passedTestsCount"],
                  node["failedTestsCount"], node["notRunTestsCount"],
                  [(t["node"]["name"], t["node"]["status"], t["node"]["output"])
                   for t in node["tests"]["edges"]])


def print_row(client, build):
    edges = client.builds()["data"]["projects"]["edges"][0]["node"]["builds"]["edges"]
    matching = [e["node"] for e in edges if e["node"]["name"] == build]
    print(f"  rows for {build}: {len(matching)}")
    for node in matching:
        print("  ", node["id"], "pass/fail/notrun", node["passedTestsCount"],
              node["failedTestsCount"], node["notRunTestsCount"],
              [(t["node"]["name"], t["node"]["status"], t["node"]["output"])
               for t in node["tests"]["edges"]], flush=True)


def check_replacement_fix(client, gap_seconds):
    """Test two sequences. Hypothesis: Done must be sent after its Test.xml has been parsed."""
    pending = [("slurm-job", "notrun", "PENDING job 801")]
    running = [("slurm-job", "notrun", "RUNNING job 801")]
    final = [("test_ok", "passed", "fine"), ("slurm-job", "passed", "COMPLETED")]

    print("== A: pending then final, Done sent immediately (the simple design)")
    build, stamp = "spike-two-step", "20261105-0700-Experimental"
    for label, tests in (("pending", pending), ("final", final)):
        client.put("Test.xml", test_xml(build, stamp, tests, "801"), build, stamp)
        client.put("Done.xml", done_xml(stamp), build, stamp)
        client.wait(label, gap_seconds)
        print_row(client, build)

    print("== B: pending, running, final, waiting for parsing before each Done")
    build, stamp = "spike-three-step", "20261105-0800-Experimental"
    for label, tests in (("pending", pending), ("running", running), ("final", final)):
        client.put("Test.xml", test_xml(build, stamp, tests, "801"), build, stamp)
        client.wait(f"{label} parse", gap_seconds)
        client.put("Done.xml", done_xml(stamp), build, stamp)
        client.wait(f"{label} done", gap_seconds)
        print_row(client, build)


def wait_for_row(client, build, expected_outputs, timeout_seconds=1200, poll_seconds=15):
    """Poll until the row's test outputs equal expected_outputs. Returns seconds, or None."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        edges = client.builds()["data"]["projects"]["edges"][0]["node"]["builds"]["edges"]
        rows = [e["node"] for e in edges if e["node"]["name"] == build]
        outputs = sorted(t["node"]["output"] for row in rows for t in row["tests"]["edges"])
        if len(rows) == 1 and outputs == sorted(expected_outputs):
            return round(time.time() - start)
        time.sleep(poll_seconds)
    return None


def check_update_twice(client, build, stamp, done_gap_seconds):
    """Pending, running, final. Done is sent only once the row shows the new content."""
    steps = [
        ("pending", [("slurm-job", "notrun", "PENDING job 901")]),
        ("running", [("slurm-job", "notrun", "RUNNING job 901")]),
        ("final", [("test_ok", "passed", "fine"), ("slurm-job", "passed", "COMPLETED")]),
    ]
    for label, tests in steps:
        client.put("Test.xml", test_xml(build, stamp, tests, "901"), build, stamp)
        seconds = wait_for_row(client, build, [t[2] for t in tests])
        print(f"{build} {label}: row correct after {seconds}s" if seconds is not None
              else f"{build} {label}: row NOT correct after 20 minutes", flush=True)
        print_row(client, build)
        client.put("Done.xml", done_xml(stamp), build, stamp)
        client.wait(f"{label} done", done_gap_seconds)
    print_row(client, build)


def junit_variant(name, stamp, wrap_site):
    """Real pytest 9.2 output, adjusted only as far as each variant says."""
    body = (
        f'<testsuite name="{name}" errors="1" failures="1" skipped="1" tests="4" time="1.5" '
        f'timestamp="2026-11-05T16:33:17" hostname="{SITE}">'
        '<testcase classname="test_s" name="test_good" time="0.5" />'
        '<testcase classname="test_s" name="test_broken" time="0.5">'
        '<failure message="assert 1 == 2" TYPEHERE>E assert 1 == 2</failure></testcase>'
        '<testcase classname="test_s" name="test_err" time="0.5">'
        '<error message="fixture boom" TYPEHERE>E RuntimeError: fixture boom</error></testcase>'
        '<testcase classname="test_s" name="test_skip" time="0.0">'
        '<skipped type="pytest.skip" message="no mpi">no mpi</skipped></testcase></testsuite>'
    )
    if wrap_site:
        body = (f'<Site BuildName="{name}" BuildStamp="{stamp}" Name="{SITE}" '
                f'Generator="radical-spike">{body}</Site>')
        return f'<?xml version="1.0" encoding="utf-8"?>{body}'
    return f'<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">{body}</testsuites>'


def check_junit(client):
    """Find the smallest change that makes pytest's JUnit parse on CDash."""
    stamp = "20261105-0900-Experimental"
    with_type = 'type="spike"'
    variants = [
        ("spike-j0-unchanged", junit_variant("spike-j0-unchanged", stamp, False).replace(" TYPEHERE", "")),
        ("spike-j1-type", junit_variant("spike-j1-type", stamp, False).replace("TYPEHERE", with_type)),
        ("spike-j2-site", junit_variant("spike-j2-site", stamp, True).replace("TYPEHERE", with_type)),
        ("spike-j3-site-notype", junit_variant("spike-j3-site-notype", stamp, True).replace(" TYPEHERE", "")),
    ]
    for build, body in variants:
        client.put("Test.xml", body, build, stamp)
    client.wait("junit", 240)
    print_builds(client.builds())


def configure_xml(build, stamp, message):
    """A Configure.xml whose log counts as one warning, which colours that cell orange."""
    start = epoch(stamp)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Site BuildName="{build}" BuildStamp="{stamp}" Name="{SITE}" Generator="radical-spike">
<Configure><StartDateTime>{stamp}</StartDateTime><StartConfigureTime>{start}</StartConfigureTime>
<ConfigureCommand>sbatch</ConfigureCommand><Log>Warning: {escape(message)}</Log>
<ConfigureStatus>0</ConfigureStatus><EndDateTime>{stamp}</EndDateTime>
<EndConfigureTime>{start}</EndConfigureTime><ElapsedMinutes>0</ElapsedMinutes></Configure></Site>"""


def print_cells(client, build):
    """What the dashboard row shows for this build."""
    for group in client.index(client.project, "2026-11-05").get("buildgroups", []):
        for row in group.get("builds", []):
            if row.get("buildname") == build:
                print("  row cells: configure", row.get("configure"), "test", row.get("test"), flush=True)


def check_distinct_states(client, gap_seconds):
    """Pending in the Configure column, running in Not Run, then a single replacement."""
    build, stamp = "spike-distinct", "20261105-1200-Experimental"
    print("== pending: Configure.xml only, no Done")
    client.put("Configure.xml", configure_xml(build, stamp, "PENDING job 951"), build, stamp)
    client.wait("pending", gap_seconds)
    print_row(client, build)
    print_cells(client, build)

    print("== running: Test.xml added to the same row")
    running = [("slurm-job", "notrun", "RUNNING job 951")]
    client.put("Test.xml", test_xml(build, stamp, running, "951"), build, stamp)
    seconds = wait_for_row(client, build, [t[2] for t in running])
    print(f"running row correct after {seconds}s", flush=True)
    print_cells(client, build)
    client.put("Done.xml", done_xml(stamp), build, stamp)
    client.wait("running done", gap_seconds)

    print("== final: replaces the whole row")
    final = [("test_ok", "passed", "fine"), ("slurm-job", "passed", "COMPLETED")]
    client.put("Test.xml", test_xml(build, stamp, final, "951"), build, stamp)
    seconds = wait_for_row(client, build, [t[2] for t in final])
    print(f"final row correct after {seconds}s", flush=True)
    client.put("Done.xml", done_xml(stamp), build, stamp)
    client.wait("final done", gap_seconds)
    print_row(client, build)
    print_cells(client, build)


def check_recovery(client, build, stamp, gap_seconds):
    """Can a row left broken by an early update be repaired by sending Done, then the result?"""
    print("== broken row before repair")
    print_row(client, build)
    client.put("Done.xml", done_xml(stamp), build, stamp)
    client.wait("done", gap_seconds)
    final = [("test_ok", "passed", "fine"), ("slurm-job", "passed", "COMPLETED")]
    client.put("Test.xml", test_xml(build, stamp, final, "901"), build, stamp)
    seconds = wait_for_row(client, build, [t[2] for t in final])
    print(f"== repaired row correct after {seconds}s" if seconds is not None
          else "== row NOT repaired after 20 minutes", flush=True)
    print_row(client, build)
    client.put("Done.xml", done_xml(stamp), build, stamp)


def check_protocol(client, gap_seconds):
    """The agreed sequence, with the version carried through the final replacement."""
    build, stamp, version = "spike-protocol", "20261105-1300-Experimental", "0.6.0"

    def show(label):
        print(f"== {label}")
        print_row(client, build)
        for group in client.index(client.project, "2026-11-05").get("buildgroups", []):
            for row in group.get("builds", []):
                if row.get("buildname") == build:
                    test = row.get("test") or {}
                    print("  row: update", (row.get("update") or {}).get("files"),
                          "configure", row.get("configure") is not None,
                          "pass/fail/notrun", test.get("pass"), test.get("fail"), test.get("notrun"),
                          flush=True)

    print("pending: Configure and Update, no Done")
    client.put("Configure.xml", configure_xml(build, stamp, "submitted job 1001"), build, stamp)
    client.put("Update.xml", update_xml(build, stamp, version), build, stamp)
    client.wait("pending", gap_seconds)
    show("pending")

    print("running: Test added, Done once visible")
    running = [("slurm-job", "notrun", "RUNNING job 1001")]
    client.put("Test.xml", test_xml(build, stamp, running, "1001"), build, stamp)
    print("visible after", wait_for_row(client, build, [t[2] for t in running]), "s", flush=True)
    client.put("Done.xml", done_xml(stamp), build, stamp)
    client.wait("running", gap_seconds)
    show("running")

    print("final: Update first, then Test once replaced, then Done")
    final = [("test_ok", "passed", "fine"), ("slurm-job", "passed", "COMPLETED")]
    client.put("Update.xml", update_xml(build, stamp, version), build, stamp)
    client.wait("update", gap_seconds)
    show("after final Update")
    client.put("Test.xml", test_xml(build, stamp, final, "1001"), build, stamp)
    print("visible after", wait_for_row(client, build, [t[2] for t in final]), "s", flush=True)
    client.put("Done.xml", done_xml(stamp), build, stamp)
    client.wait("final", gap_seconds)
    show("final")


def check_version_label(client):
    """Can a long version be shown in full? Update revisions are cut to six characters."""
    build, stamp, version = "spike-label", "20261105-0600-Experimental", "0.10.12"
    body = test_xml(build, stamp, [("test_ok", "passed", "fine")], "701")
    labels = f"<Labels><Label>rhapsody-{version}</Label></Labels>"
    body = body.replace("<Testing>", f"{labels}<Testing>")
    body = body.replace("</Results></Test>", f"</Results>{labels}</Test>")
    client.put("Update.xml", update_xml(build, stamp, version), build, stamp)
    client.put("Test.xml", body, build, stamp)
    client.put("Done.xml", done_xml(stamp), build, stamp)
    client.wait("label", 60)
    data = client.index(client.project, "2026-11-05")
    print("displaylabels:", data.get("displaylabels"))
    for group in data.get("buildgroups", []):
        for row in group.get("builds", []):
            if row.get("buildname") == build:
                print("label:", row.get("label"), "update:", row.get("update"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-replacement-fix", type=int, metavar="GAP_SECONDS",
                        help="only test the two candidate replacement sequences")
    parser.add_argument("--check-update-twice", nargs=3, metavar=("BUILD", "STAMP", "DONE_GAP"),
                        help="pending, running, final, sending Done once the row has updated")
    parser.add_argument("--check-distinct-states", type=int, metavar="GAP_SECONDS",
                        help="pending in the Configure column, running in Not Run, one replacement")
    parser.add_argument("--check-protocol", type=int, metavar="GAP_SECONDS",
                        help="run the complete agreed sequence, including the version")
    parser.add_argument("--check-recovery", nargs=3, metavar=("BUILD", "STAMP", "GAP"),
                        help="repair a broken row: send Done, wait, then post the final result")
    parser.add_argument("--check-junit", action="store_true",
                        help="only test which JUnit variants CDash parses")
    parser.add_argument("--check-version-label", action="store_true",
                        help="only run the long-version display check")
    parser.add_argument("--retest-replacement", type=int, metavar="GAP_SECONDS",
                        help="only rerun the replacement check, with this gap between steps")
    parser.add_argument("--project", required=True, help="sandbox CDash project")
    parser.add_argument("--dry-run", action="store_true", help="write payloads, send nothing")
    parser.add_argument("--out", type=pathlib.Path, help="output directory for --dry-run")
    parser.add_argument("--report-only", action="store_true", help="read results, send nothing")
    args = parser.parse_args()

    if args.report_only:
        report(Client(args.project, None))
        return

    if args.project == PRODUCTION_PROJECT:
        sys.exit(f"Refusing to submit spike data to the production project {PRODUCTION_PROJECT}.")
    if args.dry_run and not args.out:
        sys.exit("--dry-run needs --out")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    client = Client(args.project, args.out if args.dry_run else None)

    if args.retest_replacement:
        retest_replacement(client, args.retest_replacement)
        return
    if args.check_version_label:
        check_version_label(client)
        return
    if args.check_update_twice:
        build, stamp, gap = args.check_update_twice
        check_update_twice(client, build, stamp, int(gap))
        return
    if args.check_junit:
        check_junit(client)
        return
    if args.check_protocol:
        check_protocol(client, args.check_protocol)
        return
    if args.check_recovery:
        build, stamp, gap = args.check_recovery
        check_recovery(client, build, stamp, int(gap))
        return
    if args.check_distinct_states:
        check_distinct_states(client, args.check_distinct_states)
        return
    if args.check_replacement_fix:
        check_replacement_fix(client, args.check_replacement_fix)
        return

    def post_test(build, stamp, tests, job_id):
        client.put("Test.xml", test_xml(build, stamp, tests, job_id), build, stamp)

    def post_done(build, stamp):
        client.put("Done.xml", done_xml(stamp), build, stamp)

    print("1. Replacement: pending, then running, then final, each followed by Done")
    post_test("spike-replace", REPLACE_STAMP, [("slurm-job", "notrun", "PENDING job 111")], "111")
    post_done("spike-replace", REPLACE_STAMP)
    print("2. Control: placeholder with no Done before the final result")
    post_test("spike-nodone", NO_DONE_STAMP, [("slurm-job", "notrun", "PENDING job 222")], "222")
    client.wait("placeholders", 45)
    if not args.dry_run:
        print_builds(client.builds())

    post_test("spike-replace", REPLACE_STAMP, [("slurm-job", "notrun", "RUNNING job 111")], "111")
    post_done("spike-replace", REPLACE_STAMP)
    client.wait("running update", 30)
    if not args.dry_run:
        print_builds(client.builds())

    final = [("test_ok", "passed", "fine"), ("test_bad", "failed", "AssertionError: boom"),
             ("slurm-job", "passed", "COMPLETED")]
    post_test("spike-replace", REPLACE_STAMP, final, "111")
    post_done("spike-replace", REPLACE_STAMP)
    post_test("spike-nodone", NO_DONE_STAMP, final, "222")
    post_done("spike-nodone", NO_DONE_STAMP)

    print("3. Stable name across two releases, custom track, version in Update revision")
    client.put("Update.xml", update_xml("spike-stable", RELEASE_1_STAMP, "v1.0.0"),
               "spike-stable", RELEASE_1_STAMP)
    post_test("spike-stable", RELEASE_1_STAMP,
              [("test_a", "passed", "ok"), ("test_b", "passed", "ok")], "301")
    post_done("spike-stable", RELEASE_1_STAMP)
    client.wait("release 1", 30)
    client.put("Update.xml", update_xml("spike-stable", RELEASE_2_STAMP, "v1.1.0"),
               "spike-stable", RELEASE_2_STAMP)
    post_test("spike-stable", RELEASE_2_STAMP,
              [("test_a", "passed", "ok"), ("test_b", "failed", "TIMEOUT: killed by SLURM")], "302")
    post_done("spike-stable", RELEASE_2_STAMP)

    print("4. Raw pytest-style JUnit")
    client.put("Test.xml", JUNIT, "spike-junit", JUNIT_STAMP)

    print("5. Output size limit")
    post_test("spike-large", LARGE_STAMP, [("large", "failed", "x" * LARGE_OUTPUT_BYTES)], "501")
    post_done("spike-large", LARGE_STAMP)

    if args.dry_run:
        print(f"Dry run complete: {client.sent} payloads in {args.out}")
        return

    client.wait("finals", 60)
    report(client)


if __name__ == "__main__":
    main()
