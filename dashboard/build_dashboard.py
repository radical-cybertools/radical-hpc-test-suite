#!/usr/bin/env python3
"""Generate the Grafana dashboard JSON: an overview card per machine, then each machine's details.

Public dashboards can't use variables, so each machine's panels and query are written out.
Connecting a machine means moving it from --planned to --machine and rerunning this.

    build_dashboard.py --datasource <uid> --out hpc-monitor.json --planned Delta \
        --machine StellarAI=https://raw.githubusercontent.com/radical-cybertools/radical-hpc-test-suite/results/data/stellar-ai.csv

The name before "=" is what the dashboard shows. Each machine's tests and usage are read from the same place, from <machine>-tests.csv and
<machine>-usage.csv. A --machine value that is a local file is embedded in the dashboard instead
of fetched. --push sends the result to a Grafana stack.
"""

import argparse
import json
import pathlib
import urllib.request

COLUMNS = (
    "run_id", "machine", "software", "version", "slurm_job_id", "status", "slurm_state",
    "failure_summary", "passed", "failed", "skipped", "duration_min", "nodes",
    "dragon_version", "submitted_at", "ended_at", "details_url", "last_reported",
    "cpu_percent", "peak_memory_gb", "cpu_hours", "suite", "run_label",
)
TEST_COLUMNS = (
    "run_id", "machine", "version", "suite", "slurm_job_id", "run_status", "slurm_state", "passed",
    "failed", "skipped", "duration_min", "cpu_percent", "peak_memory_gb", "dragon_version", "test",
    "result", "outcome", "duration_s", "error", "details_url", "submitted_at",
)
USAGE_COLUMNS = (
    "run_id", "machine", "version", "suite", "slurm_job_id", "test_file", "elapsed_s", "cpu_percent",
    "peak_memory_gb", "submitted_at",
)
ALL_COLUMNS = {*COLUMNS, *TEST_COLUMNS, *USAGE_COLUMNS}
NUMBERS = {"passed", "failed", "skipped", "duration_min", "duration_s", "elapsed_s", "cpu_percent",
           "peak_memory_gb", "cpu_hours"}
TIMES = {"submitted_at", "ended_at", "last_reported"}
STATUS_COLORS = {"PASSED": "green", "RUNNING": "yellow", "PENDING": "yellow", "FAILED": "red",
                 "SKIPPED": "gray"}
STATUS_MAP = [{"type": "value", "options": {
    name: {"color": color, "index": index, "text": name}
    for index, (name, color) in enumerate(STATUS_COLORS.items())
}}]
COLOR_CELL = {"type": "color-background", "mode": "basic"}
WRAP = {"type": "auto", "wrapText": True}
CARD_WIDTH = 6
CARD_HEIGHT = 15


def field_type(column):
    return "number" if column in NUMBERS else "timestamp" if column in TIMES else "string"


class Builder:
    def __init__(self, datasource):
        self.datasource = {"uid": datasource, "type": "yesoreyeram-infinity-datasource"}
        self.next_id = 0

    def query(self, ref, source, columns=COLUMNS):
        query = {
            "refId": ref, "datasource": self.datasource, "type": "csv", "format": "table",
            "parser": "backend", "root_selector": "", "filters": [], "csv_options": {},
            "columns": [{"selector": c, "text": c, "type": field_type(c)} for c in columns],
            "url": "", "url_options": {"method": "GET", "data": ""},
        }
        if source.startswith(("http://", "https://")):
            return query | {"source": "url", "url": source}
        return query | {"source": "inline", "data": pathlib.Path(source).read_text()}

    def panel(self, kind, title, x, y, w, h, targets, **extra):
        self.next_id += 1
        return {"id": self.next_id, "type": kind, "title": title, "datasource": self.datasource,
                "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets, **extra}

    def row(self, title, y, panels=None):
        self.next_id += 1
        return {"id": self.next_id, "type": "row", "title": title, "collapsed": panels is not None,
                "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}

    def stat(self, title, x, y, w, h, target, field, status=False, transformations=(), **defaults):
        if status:
            defaults["mappings"] = STATUS_MAP
        return self.panel(
            "stat", title, x, y, w, h, [target], transformations=list(transformations),
            options={
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": f"/^{field}$/", "values": False},
                "colorMode": "background" if status else "none", "graphMode": "none",
                "textMode": "value", "justifyMode": "center",
            },
            fieldConfig={"defaults": defaults, "overrides": []},
        )


def keep(*names, rename=None):
    return {"id": "organize", "options": {
        "excludeByName": {c: True for c in ALL_COLUMNS if c not in names},
        "indexByName": {name: i for i, name in enumerate(names)}, "renameByName": rename or {},
    }}


def newest_first(field="submitted_at"):
    return {"id": "sortBy", "options": {"sort": [{"field": field, "desc": True}]}}


LATEST_RUN = [newest_first(), {"id": "limit", "options": {"limitField": 1}}]


def calculate(left, operator, right, alias):
    return {"id": "calculateField", "options": {
        "mode": "binary", "alias": alias, "replaceFields": False,
        "binary": {"left": left, "operator": operator, "right": right},
        "reduce": {"reducer": "sum"},
    }}


def override(name, *properties):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k, "value": v} for k, v in properties]}


def logs_override():
    return override("details_url", ("displayName", "logs"), ("custom.width", 70),
                    ("links", [{"title": "Open logs and JUnit", "url": "${__value.raw}",
                                "targetBlank": True}]),
                    ("mappings", [{"type": "regex", "options": {
                        "pattern": ".+", "result": {"text": "open", "index": 0}}}]))


def run_row_fields():
    """Columns of the expandable run rows. Two stay flexible, so the nested table gets full width."""
    return {"defaults": {"custom": {"filterable": True}}, "overrides": [
        override("version", ("custom.width", 90)),
        override("suite", ("custom.width", 70)),
        override("slurm_job_id", ("displayName", "job"), ("custom.width", 90)),
        override("run_status", ("displayName", "status"), ("mappings", STATUS_MAP),
                 ("custom.width", 110), ("custom.cellOptions", COLOR_CELL)),
        override("slurm_state", ("displayName", "SLURM state")),
        override("submitted_at", ("displayName", "submitted")),
        override("passed", ("custom.width", 90)),
        override("failed", ("custom.width", 90)),
        override("skipped", ("custom.width", 90)),
        override("duration_min", ("displayName", "min"), ("custom.width", 80)),
        override("cpu_percent", ("displayName", "CPU use"), ("unit", "percent"),
                 ("custom.width", 100)),
        override("peak_memory_gb", ("displayName", "memory (GiB)"), ("custom.width", 130)),
        override("dragon_version", ("displayName", "Dragon"), ("custom.width", 90)),
        logs_override(),
    ]}


def table_fields(machine_names=()):
    """Formatting for the failures and per-file tables, showing machines by display name."""
    gauge = {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}
    names = {data: {"text": shown, "index": i} for i, (data, shown) in enumerate(machine_names)}
    return {"defaults": {"custom": {"filterable": True}}, "overrides": [
        override("machine", ("mappings", [{"type": "value", "options": names}]),
                 ("custom.width", 140)),
        override("failure_summary", ("displayName", "why it failed"), ("custom.cellOptions", WRAP)),
        override("version", ("custom.width", 100)),
        override("suite", ("custom.width", 70)),
        override("slurm_job_id", ("displayName", "job"), ("custom.width", 100)),
        override("test_file", ("displayName", "test file"), ("custom.width", 200)),
        override("elapsed_s", ("displayName", "time (s)"), ("custom.width", 90)),
        override("cpu_percent", ("displayName", "CPU use of allocation"), ("unit", "percent"),
                 ("min", 0), ("max", 100), ("color", {"mode": "fixed", "fixedColor": "blue"}),
                 ("custom.cellOptions", gauge)),
        override("peak_memory_gb", ("displayName", "memory (GiB)"), ("custom.width", 130)),
        logs_override(),
    ]}


def machine_card(builder, name, source, x, y):
    query = builder.query("A", source)
    rate = [*LATEST_RUN, calculate("passed", "+", "failed", "ran"),
            calculate("passed", "/", "ran", "pass_rate")]
    colors = (("passed", "green"), ("failed", "red"), ("skipped", "orange"))
    return [
        builder.stat(name, x, y, CARD_WIDTH, 3, query, "pass_rate",
                     transformations=rate, unit="percentunit", decimals=1, noValue="no results yet"),
        builder.panel(
            "piechart", "", x, y + 3, CARD_WIDTH, 7, [query],
            transformations=[*LATEST_RUN, keep("passed", "failed", "skipped")],
            options={"pieType": "donut", "displayLabels": [],
                     "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "tooltip": {"mode": "single"},
                     "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom",
                                "values": ["value"]}},
            fieldConfig={"defaults": {}, "overrides": [
                override(field, ("color", {"mode": "fixed", "fixedColor": color}))
                for field, color in colors
            ]},
        ),
        builder.stat("Latest run", x, y + 10, CARD_WIDTH // 2, 3, query, "status", status=True,
                     transformations=LATEST_RUN),
        builder.panel(
            "table", "Last report", x + CARD_WIDTH // 2, y + 10, CARD_WIDTH // 2, 3, [query],
            transformations=[newest_first("last_reported"),
                             {"id": "limit", "options": {"limitField": 1}}, keep("last_reported")],
            fieldConfig={"defaults": {"unit": "dateTimeFromNow", "custom": {"align": "center"}},
                         "overrides": []},
            options={"showHeader": False},
        ),
        # Which run the status describes, such as "0.6.0 GPU", from the same newest run.
        builder.panel(
            "stat", "", x, y + 13, CARD_WIDTH, 2, [query], transformations=LATEST_RUN,
            options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/^run_label$/",
                                       "values": False},
                     "colorMode": "none", "graphMode": "none", "textMode": "value",
                     "justifyMode": "center", "text": {"valueSize": 16}},
            fieldConfig={"defaults": {"noValue": "-"}, "overrides": []},
        ),
    ]


def planned_card(builder, name, x, y):
    content = ("<div style='text-align:center;margin-top:90px;color:#8e8e8e'>"
               "<h2>Not connected</h2>No results yet</div>")
    panel = builder.panel("text", name, x, y, CARD_WIDTH, CARD_HEIGHT, [],
                          options={"mode": "html", "content": content})
    panel.pop("datasource")
    panel.pop("targets")
    return panel


def tests_chart(builder, x, y, w, h, target):
    colors = (("passed", "green"), ("failed", "red"), ("skipped", "gray"))
    return builder.panel(
        "barchart", "Tests per release", x, y, w, h, [target],
        transformations=[keep("run_label", "passed", "failed", "skipped")],
        options={"xField": "run_label", "stacking": "normal", "showValue": "never",
                 "orientation": "vertical", "tooltip": {"mode": "multi"},
                 "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}},
        fieldConfig={"defaults": {"custom": {"fillOpacity": 90, "lineWidth": 0}}, "overrides": [
            override(name, ("color", {"mode": "fixed", "fixedColor": color}))
            for name, color in colors
        ]},
    )


def machine_details(builder, name, source, y):
    """Return one machine's section and the next free row."""
    query = builder.query("A", source)
    base = source.removesuffix(".csv")
    tests = builder.query("A", f"{base}-tests.csv", TEST_COLUMNS)
    usage = builder.query("A", f"{base}-usage.csv", USAGE_COLUMNS)
    run_fields = ("submitted_at", "version", "suite", "slurm_job_id", "run_status", "slurm_state", "passed",
                  "failed", "skipped", "duration_min", "cpu_percent", "peak_memory_gb",
                  "dragon_version", "details_url")
    nested = {"id": "groupToNestedTable", "options": {
        "showSubframeHeaders": True,
        "fields": {c: {"aggregations": [], "operation": "groupby"} for c in run_fields},
    }}
    panels = [
        builder.row(name, y),
        builder.panel("table", "Runs, newest first: click the arrow to see every test",
                      0, y + 1, 24, 14, [tests],
                      # Field overrides don't reach nested tables, so rename before grouping.
                      transformations=[keep(*run_fields, "outcome", "test", "duration_s", "error",
                                            rename={"outcome": "test result",
                                                    "duration_s": "time (s)"}), nested],
                      fieldConfig=run_row_fields()),
        tests_chart(builder, 0, y + 15, 10, 9, query),
        builder.panel("table", "Resources per test file, newest run first", 10, y + 15, 14, 9, [usage],
                      transformations=[keep("suite", "slurm_job_id", "test_file", "elapsed_s",
                                            "cpu_percent", "peak_memory_gb")],
                      fieldConfig=table_fields()),
    ]
    return panels, y + 24


def data_name(source):
    """The machine's name in its data, which is the CSV file's name."""
    return pathlib.PurePosixPath(source).stem


def build(machines, planned, datasource, uid, title):
    builder = Builder(datasource)
    panels = [builder.row("HPC status overview", 0)]
    cards = [*machines, *((name, None) for name in planned)]
    for index, (name, source) in enumerate(cards):
        x = CARD_WIDTH * (index % 4)
        y = 1 + CARD_HEIGHT * (index // 4)
        if source:
            panels += machine_card(builder, name, source, x, y)
        else:
            panels.append(planned_card(builder, name, x, y))
    y = 1 + CARD_HEIGHT * ((len(cards) + 3) // 4)

    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    every = [builder.query(letters[i], source) for i, (_, source) in enumerate(machines)]
    machine_names = [(data_name(source), name) for name, source in machines]
    merge = {"id": "merge", "options": {}}
    panels.append(builder.panel(
        "table", "Failures on all machines, newest first", 0, y, 24, 7, every,
        transformations=[
            merge,
            {"id": "filterByValue", "options": {"type": "include", "match": "any", "filters": [
                {"fieldName": "status", "config": {"id": "equal", "options": {"value": "FAILED"}}}]}},
            newest_first(),
            keep("machine", "version", "suite", "slurm_job_id", "failure_summary", "details_url"),
        ],
        fieldConfig=table_fields(machine_names),
    ))
    y += 7
    for name, source in machines:
        details, y = machine_details(builder, name, source, y)
        panels += details
    panels.append(builder.row("All machines by release", y, [
        builder.panel("table", "Status of each release on each machine", 0, y + 1, 24,
                      2 + 2 * len(machines), every,
                      transformations=[merge, {"id": "groupingToMatrix", "options": {
                          "columnField": "version", "rowField": "machine",
                          "valueField": "status", "emptyValue": "empty"}}],
                      fieldConfig={"defaults": {"mappings": STATUS_MAP, "custom": {
                          "align": "center", "cellOptions": COLOR_CELL}},
                          "overrides": [override(
                              "machine\\version", ("displayName", "machine"),
                              ("custom.cellOptions", {"type": "auto"}),
                              ("mappings", [{"type": "value", "options": {
                                  data: {"text": shown, "index": i}
                                  for i, (data, shown) in enumerate(machine_names)}}]))]}),
    ]))
    return {"uid": uid, "title": title, "schemaVersion": 39, "panels": panels, "refresh": "5m",
            "timezone": "utc", "time": {"from": "now-1h", "to": "now"}}


def push(dashboard, stack, token_file):
    token = pathlib.Path(token_file).expanduser().read_text().strip()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    stack = stack.rstrip("/")

    def call(path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(stack + path, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    existing = call(f"/api/dashboards/uid/{dashboard['uid']}")
    payload = dashboard | {"id": existing["dashboard"]["id"]}
    result = call("/api/dashboards/db", {"dashboard": payload, "overwrite": True,
                                         "message": "build_dashboard.py"})
    print(f"pushed version {result['version']} to {stack}{result['url']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--machine", action="append", required=True, metavar="NAME=CSV")
    parser.add_argument("--planned", action="append", default=[], metavar="NAME",
                        help="a machine to show as a grey 'not connected' card")
    parser.add_argument("--datasource", required=True, help="Infinity data source UID")
    parser.add_argument("--uid", default="dashboard-spike")
    parser.add_argument("--title", default="RADICAL-Cybertools Cross-HPC Test Dashboard")
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--push", metavar="STACK_URL")
    parser.add_argument("--token-file", default="~/.hpc-monitor-grafana/grafana.token")
    args = parser.parse_args()

    machines = [tuple(item.split("=", 1)) for item in args.machine]
    dashboard = build(machines, args.planned, args.datasource, args.uid, args.title)
    if args.out:
        args.out.write_text(json.dumps(dashboard, indent=1) + "\n")
    if args.push:
        push(dashboard, args.push, args.token_file)


if __name__ == "__main__":
    main()
