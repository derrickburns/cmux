#!/usr/bin/env python3
"""No manaflow-ai job runs on a GitHub-hosted Mac.

Leo, 2026-10-27: "THE GITHUB HOSTED MACS / ARE THE SLOWEST THINGS / THEY PREVIOUSLY WREN'T EVEN UNDER
CONSIDERATINO". The routing order for every compile, build and test is Linux, then a free owned Mac, then
Blacksmith macOS as the last resort. GitHub-hosted macOS is not in it, not even as a fallback.

A hosted label reaches runs-on three ways, and this test reads all three:
- a literal in a workflow (runs-on, a matrix row, a pool list, an env or input default);
- a label a script computes and a job reads from an output (next_macos_cascade.py's old `HOSTED =
  "macos-26"` overflow), so every quoted hosted label in scripts/ and .github/actions counts too;
- the capability map's manaflow-ai fleet (.github/runners.json), which resolve-runners.yml publishes.

Allowed: the fork branch `github.repository_owner != 'manaflow-ai' && '<label>'` (it never evaluates in
manaflow-ai; a Blacksmith label would queue forever in a personal fork), comments, and the exact lines in
EXEMPT, each with the reason it cannot move. Python returns behind the same proven owner inequality
(or its pure, unshadowed zero-argument helper) are fork-only too. This admits individual literals,
never an entire script or line. A `hosted_runner:` matrix row is not exempt: nothing proves
every reader of it sits behind the fork branch, and no workflow has one today.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
ACTIONS = ROOT / ".github/actions"
SCRIPTS = ROOT / "scripts"
RUNNERS = ROOT / ".github/runners.json"

# A GitHub-hosted macOS image label: macos-latest, macos-15, macos-26, macos-15-intel, macos-15-xlarge, ...
# The look-behind keeps owned and Blacksmith labels out (blacksmith-6vcpu-macos-26, glaeda-...-macos-26).
HOSTED_MACOS = re.compile(r"(?<![A-Za-z0-9_.-])macos-(?:latest|[0-9]+)(?:-(?:intel|large|xlarge|arm64))?(?![A-Za-z0-9_.-])")
# The fork branch: only a repository owner other than manaflow-ai reaches it.
FORK_BRANCH = re.compile(r"github\.repository_owner != 'manaflow-ai' && ('[A-Za-z0-9._-]+'|matrix\.hosted_runner)")
# Script test fixtures and unit tests describe hosted jobs they must recognise; they route nothing.
SCRIPT_SKIP_PARTS = {"tests", "fixtures", "__pycache__", "node_modules"}
SCRIPT_SUFFIXES = {".py", ".sh", ".rb", ".js", ".mjs", ".ts", ".json", ".yml", ".yaml"}

# "<path relative to the repository root>:<exact line>" -> why it may name a GitHub-hosted Mac.
# No job is exempt: every job has an owned or Blacksmith route, x86_64 included (Rosetta).
EXEMPT: dict[str, str] = {
    ".github/workflows/test-e2e.yml:" + "      CMUX_CI_XCODE_APP: ${{ (contains(needs.runner.outputs.label, 'macos-26') || startsWith(needs.runner.outputs.label, 'glaeda-')) && vars.CMUX_CI_XCODE_APP_PR || '' }}": (
        "reads the picked label (a Blacksmith macos-26 pool) to choose Xcode; it selects no runner"
    ),
    "scripts/ci/dispatch-focused-test.py:" + '        return runner.startswith("blacksmith-") and "macos-26" in runner': (
        "a substring test on a Blacksmith label; it returns a bool, not a label"
    ),
    "scripts/ci/dispatch-focused-test.py:" + '            if not runner or pool.pr_runner_pool.persistent(runner) or "macos-26" not in runner:': (
        "a substring test on the picked label; it selects no runner"
    ),
    "scripts/ci/resolve_runners.py:" + '            \'Expected an object of capability -> label, e.g. {"macos_26": "macos-15"}.\'': (
        "an example in the resolver's error message for a malformed fleet override; it selects no runner"
    ),
}


def strip_comment(line: str) -> str:
    """The line without a trailing YAML/shell comment (a `#` after whitespace, outside `${{ }}`)."""
    if line.lstrip().startswith("#"):
        return ""
    return re.sub(r"\s+#\s.*$", "", line)


def workflow_hits(root: Path) -> list[str]:
    hits = []
    for directory in (root / ".github/workflows", root / ".github/actions"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.suffix not in {".yml", ".yaml"}:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                content = strip_comment(line)
                if not content:
                    continue
                if HOSTED_MACOS.search(FORK_BRANCH.sub("", content)):
                    hits.append(f"{path.relative_to(root)}:{number}:{line}")
    return hits


def python_fork_literals(source: str) -> set[tuple[int, int, int]]:
    """Hosted constants behind the existing non-manaflow owner branch, fail closed.

    This is a closed source grammar, not arbitrary Python data-flow inference.
    Only guarded returns and direct comparison operands are admitted; else arms,
    escaping functions, side-effect arguments and rebound helpers stay visible.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    owner_tests = {
        ast.dump(ast.parse(f'{name}.split("/", 1)[0].casefold() != "manaflow-ai"', mode="eval").body)
        for name in ("REPO", "repository")
    }
    definitions = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    rebound = {node.id for node in ast.walk(tree)
               if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
    rebound.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    rebound.update(alias.asname or alias.name.split(".")[0]
                   for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in node.names)
    helpers = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.decorator_list or node.name in rebound:
            continue
        if sum(other.name == node.name for other in definitions) != 1:
            continue
        args = node.args
        if args.posonlyargs or args.args or args.kwonlyargs or args.vararg or args.kwarg:
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        if len(body) == 1 and isinstance(body[0], ast.Return) \
                and ast.dump(body[0].value) in owner_tests:
            helpers.add(node.name)

    def fork_guard(node: ast.AST) -> bool:
        if ast.dump(node) in owner_tests:
            return True
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return any(fork_guard(value) for value in node.values)
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in helpers and not node.args and not node.keywords)

    spans = set()

    def admit(node: ast.AST) -> None:
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and HOSTED_MACOS.fullmatch(node.value) and node.lineno == node.end_lineno):
            spans.add((node.lineno, node.col_offset, node.end_col_offset))

    def scoped_nodes(node: ast.AST):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda, ast.GeneratorExp)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from scoped_nodes(child)

    def returns(node: ast.AST) -> None:
        for value in scoped_nodes(node):
            if isinstance(value, ast.Return) and value.value is not None:
                for expression in scoped_nodes(value.value):
                    admit(expression)

    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not fork_guard(node.test):
            continue
        # A comparison reads a label; a call argument could act before the
        # neighboring owner check, so it is deliberately not admitted here.
        for comparison in scoped_nodes(node.test):
            if isinstance(comparison, ast.Compare):
                for operand in (comparison.left, *comparison.comparators):
                    admit(operand)
        for statement in node.body:
            returns(statement)
    return spans


def script_hits(root: Path) -> list[str]:
    """Quoted hosted labels in scripts: a label a script returns or writes reaches runs-on via an output."""
    quoted = re.compile(r"""["'](macos-(?:latest|[0-9]+)(?:-(?:intel|large|xlarge|arm64))?)["']""")
    hits = []
    for base in (root / "scripts", root / ".github/actions"):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SCRIPT_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if SCRIPT_SKIP_PARTS & set(relative.parts) or path.name.startswith("test_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            admitted = python_fork_literals(text) if path.suffix == ".py" else set()
            for number, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                forks = [match.span() for match in FORK_BRANCH.finditer(line)]
                for match in quoted.finditer(line):
                    if any(start <= match.start() and match.end() <= end for start, end in forks):
                        continue
                    start, end = (len(line[:offset].encode("utf-8")) for offset in match.span())
                    if any(row == number and left <= start and end <= right
                           for row, left, right in admitted):
                        continue
                    hits.append(f"{relative}:{number}:{line}")
                    break
    return hits


def fleet_hits(root: Path) -> list[str]:
    runners = json.loads((root / ".github/runners.json").read_text(encoding="utf-8"))
    fleet = runners["owners"]["manaflow-ai"]
    return [f".github/runners.json: fleet {fleet!r} maps {key} to {label}"
            for key, label in runners["fleets"][fleet].items() if HOSTED_MACOS.search(label)]


def unexempted(hits: list[str]) -> list[str]:
    left = []
    for hit in hits:
        path, _, rest = hit.partition(":")
        _, _, line = rest.partition(":")
        if f"{path}:{line}" not in EXEMPT:
            left.append(hit)
    return left


class NoGitHubHostedMacos(unittest.TestCase):
    def test_the_patterns_catch_hosted_labels_and_pass_owned_ones(self):
        for line in ("    runs-on: macos-26", "    runs-on: macos-15-intel # github-hosted-required: x",
                     "          - os: macos-15-intel", "        [{\"pool\": \"hosted\", \"runner\": \"macos-26\"}]",
                     "    runs-on: ${{ vars.X || 'macos-latest' }}", "    runs-on: macos-15-xlarge",
                     "    runs-on: ${{ github.repository_owner != 'manaflow-ai' && 'macos-26' || 'macos-26' }}"):
            with self.subTest(line=line):
                self.assertTrue(HOSTED_MACOS.search(FORK_BRANCH.sub("", strip_comment(line))))
        for line in ("    runs-on: ${{ github.repository_owner != 'manaflow-ai' && 'macos-26' || 'blacksmith-6vcpu-macos-26' }}",
                     "    runs-on: glaeda-side-light-xcode-26.6", "    runs-on: blacksmith-12vcpu-macos-26",
                     "    # runs on hosted macos-26 only in a fork", "      matrix.hosted_runner"):
            with self.subTest(line=line):
                self.assertFalse(HOSTED_MACOS.search(FORK_BRANCH.sub("", strip_comment(line))))
        # A matrix row is read like any other line: `runs-on: ${{ matrix.hosted_runner }}` would reach it.
        self.assertTrue(HOSTED_MACOS.search(FORK_BRANCH.sub("", strip_comment("          hosted_runner: macos-15"))))

    def scan_python(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts" / "any_route.py"
            script.parent.mkdir()
            script.write_text(textwrap.dedent(source), encoding="utf-8")
            return script_hits(root)

    def test_python_personal_owner_branches_are_supported(self):
        for source in (
            """
            def pick(repository):
                if repository.split("/", 1)[0].casefold() != "manaflow-ai":
                    return "macos-26"
                return "blacksmith-6vcpu-macos-26"
            """,
            """
            def pick(repository, label):
                if label == "macos-26" and repository.split("/", 1)[0].casefold() != "manaflow-ai":
                    return "macos-26"
                return None
            """,
        ):
            with self.subTest(source=source):
                self.assertEqual(self.scan_python(source), [])

    def test_python_pure_personal_owner_helper_is_supported(self):
        self.assertEqual(self.scan_python('''
            REPO = "manaflow-ai/cmux"
            def hosted_fork():
                """The same owner branch as the workflow."""
                return REPO.split("/", 1)[0].casefold() != "manaflow-ai"
            def pick():
                if hosted_fork():
                    return "macos-26"
                return "blacksmith-6vcpu-macos-26"
            def product(labels):
                if hosted_fork():
                    return "macos-26" if "macos-26" in labels else None
                return None
            def reuse(found):
                if hosted_fork() and found.get("runner") != "macos-26":
                    return None
                return found
        '''), [])

    def test_python_upstream_and_unproven_routes_stay_rejected(self):
        pure = '''
            REPO = "manaflow-ai/cmux"
            def hosted_fork():
                return REPO.split("/", 1)[0].casefold() != "manaflow-ai"
        '''
        cases = [
            'def pick():\n    return "macos-26"\n',
            'def pick(repository):\n    if repository.split("/", 1)[0].casefold() == "manaflow-ai":\n        return "macos-26"\n',
            'def pick(repository, ready):\n    if repository.split("/", 1)[0].casefold() != "manaflow-ai" or ready:\n        return "macos-26"\n',
            'def pick(repository):\n    if repository.split("/", 1)[0].casefold() != "manaflow-ai":\n        return None\n    else:\n        return "macos-26"\n',
            'def hosted_fork():\n    return True\ndef pick():\n    if hosted_fork():\n        return "macos-26"\n',
            textwrap.dedent(pure) + 'hosted_fork = lambda: True\ndef pick():\n    if hosted_fork():\n        return "macos-26"\n',
            textwrap.dedent(pure) + 'def pick(hosted_fork):\n    if hosted_fork():\n        return "macos-26"\n',
            '@replace\ndef hosted_fork():\n    return REPO.split("/", 1)[0].casefold() != "manaflow-ai"\ndef pick():\n    if hosted_fork():\n        return "macos-26"\n',
            'def pick(repository):\n    if repository.split("/", 1)[0].casefold() != "manaflow-ai":\n        def escaped():\n            return "macos-26"\n        return escaped\n',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(self.scan_python(source))

    def test_python_admission_is_per_literal_not_per_source_line(self):
        # The first call executes before the owner comparison, including upstream.
        # Its literal must not inherit the neighboring comparison/return's custody.
        hits = self.scan_python('''
            def pick(repository, label):
                if emit("macos-26") and repository.split("/", 1)[0].casefold() != "manaflow-ai" and label == "macos-26":
                    return "macos-26"
                return None
        ''')
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('emit("macos-26")', hits[0])

    def test_python_owner_branch_does_not_admit_deferred_callables(self):
        for value in ('lambda: "macos-26"', '("macos-26" for _ in range(1))'):
            with self.subTest(value=value):
                self.assertTrue(self.scan_python(
                    'def pick(repository):\n'
                    '    if repository.split("/", 1)[0].casefold() != "manaflow-ai":\n'
                    f'        return {value}\n'))

    def test_python_helper_cannot_be_rebound_by_other_syntax(self):
        prefix = ('REPO = "manaflow-ai/cmux"\n'
                  'def hosted_fork():\n'
                  '    return REPO.split("/", 1)[0].casefold() != "manaflow-ai"\n')
        tail = ('def pick():\n'
                '    if hosted_fork():\n'
                '        return "macos-26"\n')
        for binding in (
            'class hosted_fork: pass\n',
            'try: check()\nexcept Exception as hosted_fork: pass\n',
            'match supplied:\n    case {"helper": hosted_fork}: pass\n',
            'match supplied:\n    case [*hosted_fork]: pass\n',
            'match supplied:\n    case {**hosted_fork}: pass\n',
            'from another_route import *\n',
        ):
            with self.subTest(binding=binding):
                self.assertTrue(self.scan_python(prefix + binding + tail))

    def test_no_workflow_selects_a_github_hosted_mac(self):
        left = unexempted(workflow_hits(ROOT))
        self.assertEqual(left, [], "A workflow selects a GitHub-hosted Mac outside the fork branch. Route it "
                         "owned first, then Blacksmith (vars.MACOS_RUNNER_26 || 'blacksmith-6vcpu-macos-26'):\n"
                         + "\n".join(hit[:260] for hit in left))

    def test_no_script_computes_a_github_hosted_mac(self):
        left = unexempted(script_hits(ROOT))
        self.assertEqual(left, [], "A script names a GitHub-hosted Mac label, which reaches runs-on through a "
                         "job output (next_macos_cascade.py's old HOSTED overflow):\n"
                         + "\n".join(hit[:260] for hit in left))

    def test_the_manaflow_fleet_maps_no_capability_to_a_hosted_mac(self):
        self.assertEqual(fleet_hits(ROOT), [])

    def test_the_train_runner_variable_cannot_hold_a_hosted_mac(self):
        # A repository variable routes around the literal check above. The train
        # compile's runs-on no longer reads vars.NIGHTLY_NEXT_TRAIN_RUNNER: the
        # variable only reaches pick-train-runner's OWNED_LABELS, and the picker
        # (nightly_build_runner.owned_label) offers only an owned glaeda-* label,
        # so a hosted macos-* value is never picked.
        text = (WORKFLOWS / "nightly-next-train.yml").read_text(encoding="utf-8")
        reads = [line for line in text.splitlines() if "vars.NIGHTLY_NEXT_TRAIN_RUNNER" in strip_comment(line)]
        self.assertTrue(reads)
        for line in reads:
            with self.subTest(line=line):
                self.assertRegex(line, r"^\s+OWNED_LABELS: ")

    def test_no_runner_variable_may_hold_a_hosted_mac(self):
        # vars.MACOS_RUNNER_15 and friends reach runs-on in many workflows; the
        # repository-variable check and health report refuse a hosted value.
        sys.path.insert(0, str(SCRIPTS / "ci"))
        import runner_label_policy as policy

        for label in ("macos-15", "macos-26", "macos-latest", "macos-15-intel", "macos-15-xlarge", "MACOS-15"):
            with self.subTest(label=label):
                self.assertIsNotNone(policy.forbidden_reason(label))
                self.assertTrue(policy.drifted_runner_variables({"MACOS_RUNNER_15": label}))
        for label in ("blacksmith-6vcpu-macos-15", "blacksmith-12vcpu-macos-26", ""):
            with self.subTest(label=label):
                self.assertIsNone(policy.forbidden_reason(label))

    def test_every_exemption_is_live_and_has_a_reason(self):
        lines = set()
        for hit in workflow_hits(ROOT) + script_hits(ROOT):
            path, _, rest = hit.partition(":")
            lines.add(f"{path}:{rest.partition(':')[2]}")
        for key, reason in EXEMPT.items():
            with self.subTest(exemption=key):
                self.assertIn(key, lines, "a stale exemption: the line no longer exists")
                self.assertTrue(reason.strip())


if __name__ == "__main__":
    sys.exit(unittest.main())
