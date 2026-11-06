"""Checks for the design rules in docs/ARCHITECTURE.md that a test can enforce."""

import ast
import importlib.util
import pathlib
import re
import sys
import tomllib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
MONITOR_CODE = [REPO / "hpc_monitor.py", *sorted((REPO / "jobs").glob("*.py")),
                *sorted((REPO / "dashboard").glob("*.py"))]
SECRET = re.compile(
    r"BEGIN [A-Z ]*PRIVATE KEY|\bglc_\w+|\bglsa_\w+|\bghp_\w+|\bgithub_pat_\w+|"
    r"\w+\.grafana\.net|/home/\w+|/Users/\w+|(?i:\b(?:password|passwd|secret|api[_-]?key|token)\s*=\s*[\"']?\w{12,})")


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module.split(".")[0]


def outside_stdlib(paths):
    """Return {file name: modules} for imports that aren't in the standard library."""
    found = {}
    for path in paths:
        names = set(imported_modules(path)) - set(sys.stdlib_module_names)
        if names:
            found[path.name] = sorted(names)
    return found


class Code(unittest.TestCase):
    def test_monitor_code_uses_only_the_standard_library(self):
        self.assertTrue(MONITOR_CODE)
        self.assertEqual(outside_stdlib(MONITOR_CODE), {})

    def test_monitor_and_dashboard_agree_on_published_columns(self):
        monitor = load(REPO / "hpc_monitor.py")
        dashboard = load(REPO / "dashboard" / "build_dashboard.py")
        for name in ("COLUMNS", "TEST_COLUMNS", "USAGE_COLUMNS"):
            self.assertEqual(getattr(monitor, name), getattr(dashboard, name), name)


class Sites(unittest.TestCase):
    def sites(self):
        found = [p for p in (REPO / "sites").glob("*/site.toml") if p.parent.name != "_template"]
        self.assertTrue(found)
        return found

    def test_sites_publish_to_the_results_branch(self):
        for path in self.sites():
            with self.subTest(site=path.parent.name):
                config = tomllib.loads(path.read_text())
                self.assertEqual(config["publish"]["branch"], "results")
                self.assertEqual(config["machine"], path.parent.name)

    def test_job_scripts_exist(self):
        for path in self.sites():
            for run in tomllib.loads(path.read_text())["run"]:
                with self.subTest(site=path.parent.name):
                    self.assertTrue((path.parent / run["job_script"]).is_file())

    def test_nothing_personal_or_secret_in_tracked_files(self):
        paths = [*(REPO / "sites").rglob("*"), *(REPO / "docs").rglob("*.md"),
                 *(REPO / "dashboard").rglob("*"), *(REPO / "jobs").rglob("*.py"),
                 REPO / "hpc_monitor.py", REPO / "CLAUDE.md", REPO / "README.md",
                 REPO / ".claude" / "skills" / "add-machine" / "SKILL.md"]
        for path in paths:
            if path.is_file():
                with self.subTest(path=str(path.relative_to(REPO))):
                    self.assertIsNone(SECRET.search(path.read_text(errors="replace")))


class Docs(unittest.TestCase):
    def test_paths_named_in_agent_instructions_exist(self):
        pattern = re.compile(r"`((?:docs|sites|jobs|dashboard|tests)/[\w./-]+\.\w+|[\w-]+\.(?:md|py))`")
        for name in ("CLAUDE.md", ".claude/skills/add-machine/SKILL.md", "docs/ARCHITECTURE.md"):
            text = (REPO / name).read_text()
            for path in pattern.findall(text):
                with self.subTest(document=name, path=path):
                    matches = (REPO / path).exists() or list(REPO.rglob(path))
                    self.assertTrue(matches, f"{name} names {path}, which doesn't exist")


if __name__ == "__main__":
    unittest.main()
