#!/usr/bin/env python3
"""Generate the Grafana dashboard JSON, with one section per machine.

Public dashboards can't use variables, so each machine's panels and query are written out.
Adding a machine means rerunning this with one more --machine.

    build_dashboard.py --datasource <uid> --out hpc-monitor.json \
        --machine stellar-ai=https://raw.githubusercontent.com/radical-cybertools/radical-hpc-test-suite/results/data/stellar-ai.csv

Each machine's test list is read from the same place, from <machine>-tests.csv.

A --machine value that is a local file is embedded in the dashboard instead of fetched. Use
that only before the public data repo exists. --push sends the result to a Grafana stack.
"""

import argparse
import json
import pathlib
import urllib.request

COLUMNS = (
    "run_id", "machine", "software", "version", "slurm_job_id", "status", "slurm_state",
    "failure_summary", "passed", "failed", "skipped", "duration_min", "nodes",
    "dragon_version", "submitted_at", "ended_at", "details_url", "last_reported",
)
TEST_COLUMNS = (
    "run_id", "machine", "version", "slurm_job_id", "test", "result", "error", "details_url",
    "submitted_at",
)
NUMBERS = {"passed", "failed", "skipped", "duration_min"}
TIMES = {"submitted_at", "ended_at", "last_reported"}
STATUS_COLORS = {"PASSED": "green", "RUNNING": "yellow", "PENDING": "yellow", "FAILED": "red",
                 "SKIPPED": "gray"}
STATUS_MAP = [{"type": "value", "options": {
    name: {"color": color, "index": index, "text": name}
    for index, (name, color) in enumerate(STATUS_COLORS.items())
}}]


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

    def stat(self, title, x, y, w, h, target, field, status=False):
        defaults = {"mappings": STATUS_MAP} if status else {}
        return self.panel(
            "stat", title, x, y, w, h, [target],
            options={
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": f"/^{field}$/", "values": False},
                "colorMode": "background" if status else "none", "graphMode": "none",
                "textMode": "value", "justifyMode": "center",
            },
            fieldConfig={"defaults": defaults, "overrides": []},
        )


def keep(*names):
    return {"id": "organize", "options": {
        "excludeByName": {c: True for c in {*COLUMNS, *TEST_COLUMNS} if c not in names},
        "indexByName": {name: i for i, name in enumerate(names)}, "renameByName": {},
    }}


def newest_first(field="submitted_at"):
    return {"id": "sortBy", "options": {"sort": [{"field": field, "desc": True}]}}


def table_fields():
    def override(name, *properties):
        return {"matcher": {"id": "byName", "options": name},
                "properties": [{"id": k, "value": v} for k, v in properties]}

    return {"defaults": {}, "overrides": [
        override("status", ("mappings", STATUS_MAP), ("custom.width", 100),
                 ("custom.cellOptions", {"type": "color-background", "mode": "basic"})),
        override("failure_summary", ("displayName", "why it failed"), ("custom.width", 420),
                 ("custom.cellOptions", {"type": "auto", "wrapText": True})),
        override("version", ("custom.width", 80)),
        override("passed", ("custom.width", 70)),
        override("failed", ("custom.width", 70)),
        override("skipped", ("custom.width", 70)),
        override("result", ("mappings", STATUS_MAP), ("custom.width", 100),
                 ("custom.cellOptions", {"type": "color-background", "mode": "basic"})),
        override("slurm_job_id", ("custom.width", 110)),
        override("test", ("custom.width", 360),
                 ("custom.cellOptions", {"type": "auto", "wrapText": True})),
        override("error", ("custom.cellOptions", {"type": "auto", "wrapText": True})),
        override("details_url", ("displayName", "logs"), ("custom.width", 70),
                 ("links", [{"title": "Open logs and JUnit", "url": "${__value.raw}",
                             "targetBlank": True}]),
                 ("mappings", [{"type": "regex", "options": {
                     "pattern": ".+", "result": {"text": "open", "index": 0}}}])),
    ]}


def tests_chart(builder, x, y, w, h, target):
    colors = (("passed", "green"), ("failed", "red"), ("skipped", "gray"))
    return builder.panel(
        "barchart", "Tests per release", x, y, w, h, [target],
        transformations=[keep("version", "passed", "failed", "skipped")],
        options={"xField": "version", "stacking": "normal", "showValue": "never",
                 "orientation": "vertical", "tooltip": {"mode": "multi"},
                 "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}},
        fieldConfig={"defaults": {"custom": {"fillOpacity": 90, "lineWidth": 0}}, "overrides": [
            {"matcher": {"id": "byName", "options": name},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
            for name, color in colors
        ]},
    )


def duration_chart(builder, x, y, w, h, target):
    return builder.panel(
        "barchart", "Job duration per release (minutes)", x, y, w, h, [target],
        transformations=[keep("version", "duration_min")],
        options={"xField": "version", "showValue": "auto", "tooltip": {"mode": "single"},
                 "legend": {"showLegend": False}},
        fieldConfig={"defaults": {"unit": "none", "color": {"mode": "fixed", "fixedColor": "blue"},
                                  "custom": {"fillOpacity": 85, "lineWidth": 0}}, "overrides": []},
    )


def tests_source(source):
    return source.removesuffix(".csv") + "-tests.csv"


def build(machines, datasource, uid, title):
    builder = Builder(datasource)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    every = [builder.query(letters[i], source) for i, (_, source) in enumerate(machines)]
    merge = {"id": "merge", "options": {}}
    panels = [
        builder.panel("table", "All machines by release", 0, 0, 24, 2 + 2 * len(machines), every,
                      transformations=[merge, {"id": "groupingToMatrix", "options": {
                          "columnField": "version", "rowField": "machine",
                          "valueField": "status", "emptyValue": "empty"}}],
                      fieldConfig={"defaults": {"mappings": STATUS_MAP, "custom": {
                          "align": "center",
                          "cellOptions": {"type": "color-background", "mode": "basic"}}},
                          "overrides": [{
                              "matcher": {"id": "byName", "options": "machine\\version"},
                              "properties": [
                                  {"id": "displayName", "value": "machine"},
                                  {"id": "custom.cellOptions", "value": {"type": "auto"}}]}]}),
    ]
    y = 2 + 2 * len(machines)
    panels.append(builder.panel(
        "table", "Failures on all machines, newest first", 0, y, 24, 9, every,
        transformations=[
            merge,
            {"id": "filterByValue", "options": {"type": "include", "match": "any", "filters": [
                {"fieldName": "status", "config": {"id": "equal", "options": {"value": "FAILED"}}}]}},
            newest_first(),
            keep("status", "machine", "version", "failure_summary", "details_url", "slurm_state",
                 "slurm_job_id", "submitted_at"),
        ],
        fieldConfig=table_fields(),
    ))
    y += 9
    for name, source in machines:
        query = builder.query("A", source)
        builder.next_id += 1
        panels.append({"id": builder.next_id, "type": "row", "title": name, "collapsed": False,
                       "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
        y += 1
        panels += [
            builder.stat("Latest run", 0, y, 3, 4, query, "status", status=True),
            builder.stat("Release", 3, y, 3, 4, query, "version"),
            builder.stat("SLURM job", 0, y + 4, 3, 4, query, "slurm_job_id"),
            builder.panel(
                "table", "Last report", 3, y + 4, 3, 4, [query],
                transformations=[newest_first("last_reported"),
                                 {"id": "limit", "options": {"limitField": 1}},
                                 keep("last_reported")],
                fieldConfig={"defaults": {"unit": "dateTimeFromNow", "custom": {"align": "center"}},
                             "overrides": []},
                options={"showHeader": False},
            ),
            tests_chart(builder, 6, y, 9, 8, query),
            duration_chart(builder, 15, y, 9, 8, query),
            builder.panel(
                "table", f"{name}: runs, newest first", 0, y + 8, 24, 8, [query],
                transformations=[newest_first(), keep(
                    "status", "version", "failure_summary", "details_url", "slurm_state",
                    "slurm_job_id", "passed", "failed", "skipped", "dragon_version",
                    "submitted_at")],
                fieldConfig=table_fields(),
            ),
            builder.panel(
                "table", f"{name}: all tests, newest run first, failures first",
                0, y + 16, 24, 14, [builder.query("A", tests_source(source), TEST_COLUMNS)],
                transformations=[keep(
                    "version", "slurm_job_id", "result", "test", "error", "details_url")],
                fieldConfig=table_fields(),
            ),
        ]
        y += 30
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
    parser.add_argument("--datasource", required=True, help="Infinity data source UID")
    parser.add_argument("--uid", default="dashboard-spike")
    parser.add_argument("--title", default="HPC test monitor")
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--push", metavar="STACK_URL")
    parser.add_argument("--token-file", default="~/.hpc-monitor-grafana/grafana.token")
    args = parser.parse_args()

    machines = [tuple(item.split("=", 1)) for item in args.machine]
    dashboard = build(machines, args.datasource, args.uid, args.title)
    if args.out:
        args.out.write_text(json.dumps(dashboard, indent=1) + "\n")
    if args.push:
        push(dashboard, args.push, args.token_file)


if __name__ == "__main__":
    main()
