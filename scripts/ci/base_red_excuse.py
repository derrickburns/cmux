#!/usr/bin/env python3
"""Excuse a red check that fails the same way on the base branch.

Run via scripts/gh-merge-green before it refuses a red cmux-next run or a red
required check. A failed job on the PR head is excused only when the same job
of the same workflow, on the latest completed run of the base branch that has
a verdict, also failed, and the head's failed steps and failing test names are
all among the base's. Error lines are compared only when no test failed (a
compile error, say), and warnings never are: they vary run to run (SwiftPM's
cache warnings refused #18147 on 2026-10-07). Names are compared, not counts:
the base may fail more. A runner refusal (no steps, a failed setup step, or the glaeda
hook's refusal), a cancelled or timed-out job, and a failure with no test or
error line to compare are never excused. Complete static-preflight diagnostics
are also compared: their literal flag/path/date causes, never FAILED markers or
number-masked summaries. Unknown or incomplete check bodies are refused.
Dependency verdicts require the complete pinned workflow/program/needs context,
and every failed leaf still passes the ordinary base comparison. Skipped or
unreached checks remain unexecuted; attribution never reports them passing.
One base red then no longer freezes
every pull request into that base (2026-10-07: four base reds, and the PRs
fixing them deadlocked on each other's reds).

A test helper that dies on a signal names different in-flight tests and
crashed-thread frames each run, so its crash lines are not compared: the
base's job must have died on the same signal, which makes the crash the
base's (2026-10-09: a base SIGTRAP refused every feat-cmux-next PR). Failed
steps and any test red before the crash are still compared.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("main_fix_evidence", Path(__file__).with_name("main_fix_evidence.py"))
_evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_evidence)
GitHub, Refused, job_for, latest_checks = _evidence.GitHub, _evidence.Refused, _evidence.job_for, _evidence.latest_checks

SETUP_STEPS = frozenset({"Set up job", "Set up runner"})
# These names select a strict context reader, never an exemption from the audit.
DEPENDENCY_JOBS = frozenset({"linux-preflight", "ci-status"})
HOOK_REFUSAL = "glaeda-cmux-runner-hook: refused"
VERDICTS = frozenset({"success", "failure"})
TESTS = (
    re.compile(r"✘ Test (.+?\)) (?:recorded an issue|failed)"),  # Swift Testing
    re.compile(r"Test Case '-\[(\S+ \S+)\]' failed"),  # XCTest
    re.compile(r"^test (\S+) \.\.\. FAILED$"),  # cargo test
    re.compile(r"^(?:FAIL|ERROR): (\S+ \([^)]+\))"),  # unittest
    re.compile(r"^FAILED (\S+::\S+)"),  # pytest
)
ERROR = re.compile(r"##\[error\]|\berror:")
WARNING = re.compile(r"##\[warning\]|\bwarning:")
# Summaries carry counts, and a test's own lines already name it.
GENERIC = re.compile(r"Process completed with exit code|red tests?:|✘ Test |Test Case '|\.\.\. FAILED$")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
# SwiftPM's line for a killed test helper, and the hang sampler's ::error lines for it.
CRASH = re.compile(r"exited with unexpected signal code (\d+)|the test helper died on signal (\d+)")
CRASH_LINE = re.compile(r"exited with unexpected signal code|the test helper died on signal|"
                        r"no \"Test run with\" summary line")
STATIC_JOB = "Fast static checks"
STATIC_STEP = "Run the contributor static preflight"
CHECK_RESULT = re.compile(r"^(PASSED|FAILED) ([a-z][a-z0-9-]*) \(\d+(?:\.\d+)?s(?:; [^()]+)?\)$")
PREFLIGHT_END = re.compile(r"^FAILED: ([1-9]\d*)/([1-9]\d*) selected checks ran\. "
                           r"Native compilation, app tests and app launch were not checked\.$")
# This is the complete public diagnostic emitted by lint-feature-flags.py for
# an expired flag. Other failure kinds need their own complete-body parser.
EXPIRED_FLAG = re.compile(
    r"^- ((?:(?:Sources|Packages|CLI|ios)/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.swift|"
    r"web/app/lib/feature-flags\.ts): '[a-z0-9]+(?:-[a-z0-9]+)*' reviewBy "
    r"\d{4}-\d\d-\d\d has passed — remove the zombie flag or consciously extend the date)$")
CI_SOURCE = ".github/workflows/ci.yml"
CI_SOURCE_SHA256 = "e13d007598ac5617fe87a327ef5273bf97df7b189bf937e7ab8d2631c204cb8d"
CI_NEEDS = ("changes", "fast-guards", "claude-wrapper", "suite-coverage", "ui-tests",
            "static-preflight", "guards", "ghosttykit-release-check", "browser", "remote-daemon",
            "web", "linux-preflight", "macos-admission-gate", "macos")
LINUX_NEEDS = ("changes", "static-preflight", "fast-guards", "guards", "ghosttykit-release-check", "web")
CI_NAMES = {"fast-guards": "CI fast guards", "static-preflight": "Fast static checks",
            "claude-wrapper": "Claude wrapper regressions", "ghosttykit-release-check": "GhosttyKit release check",
            "macos-admission-gate": "macOS admission gate"}
CI_CALLED = frozenset({"guards", "browser", "remote-daemon", "web", "macos"})
CI_BOOLEAN_ROUTES = ("macos", "cli", "full_suite", "unit_suite", "swift_packages", "claude_wrapper",
                     "linux_guard_tests", "linux_guard_history", "linux_guard_cli", "linux_guard_source",
                     "web", "agent_session_web", "ghosttykit_release", "browser", "remote_daemon", "coverage_gap")
CI_PROGRAMS = {
    "linux-preflight": ("Check routed Linux results", "PREFLIGHT_NEEDS", 1447, 1534,
                        "49f81e56acc38ce06ae7468c11abedbd6aa6e4fa5ab26176edcdab60dae137d0"),
    "ci-status": ("Check platform workflow routing", "PLATFORM_NEEDS", 1749, 1828,
                  "4e52775680a43a6eacfe0584229e78162fb58b5eb47c2cac0ab42ce91437784c"),
}
LOG_COLOR = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*m")
RUN_IDENTITY = ("id", "head_sha", "head_branch", "workflow_id", "path", "run_attempt",
                "status", "conclusion", "event")


def lines_of(log: str) -> list[str]:
    """The text of each `job<TAB>step<TAB>stamp text` line of `gh run view --log`."""
    found = []
    for line in log.splitlines():
        parts = line.split("\t", 2)
        found.append(STAMP.sub("", parts[-1]).strip())
    return found


def signature(job: dict, log: str) -> tuple[set[str], set[str], set[str]]:
    """(failed step names, failing test names, error lines with numbers masked)."""
    steps = {step["name"] for step in job.get("steps") or []
             if step.get("conclusion") not in (None, "success", "skipped")}
    tests: set[str] = set()
    errors: set[str] = set()
    for text in lines_of(log):
        for pattern in TESTS:
            if match := pattern.search(text):
                tests.add(match.group(1))
        if ERROR.search(text) and not GENERIC.search(text) and not WARNING.search(text) and not CRASH_LINE.search(text):
            errors.add(re.sub(r"\d+", "#", text.replace("##[error]", "").strip()))
    return steps, tests, errors


def static_causes(job: dict, log: str) -> set[str]:
    """Literal causes from a complete, source-stable contributor preflight.

    Only the known expired-flag body is supported. A matching job/check name,
    generic exit code, partial body or unknown failure is never attribution.
    """
    def marker(text: str) -> bool:
        return text == "lint-feature-flags: FAILED" or text.startswith("FAILED feature-flags (")
    if job.get("name") != STATIC_JOB:
        if any(marker(text) for text in lines_of(log)):
            raise Refused("static preflight diagnostic belongs to a different job")
        return set()
    steps, _, errors = signature(job, log)
    if steps != {STATIC_STEP} or errors:
        raise Refused("static preflight has an unknown failed-step binding or error body")
    texts = []
    for line in log.splitlines():
        parts = line.split("\t", 2)
        text = STAMP.sub("", parts[-1]).strip()
        if len(parts) == 3 and (parts[0] != STATIC_JOB or parts[1] != STATIC_STEP):
            if marker(text) or parts[1] == STATIC_STEP:
                raise Refused("static preflight log has a different job or step binding")
            continue
        if text:
            texts.append(text)
    starts = [i for i, text in enumerate(texts) if CHECK_RESULT.fullmatch(text)]
    ends = [(i, match) for i, text in enumerate(texts) if (match := PREFLIGHT_END.fullmatch(text))]
    if not starts or len(ends) != 1:
        raise Refused("static preflight has no complete check-body boundary")
    end, summary = ends[0]
    if (summary[1] != summary[2] or end + 2 >= len(texts)
            or texts[end + 1] != "Next for Swift changes: use the normal native build/test workflow in CONTRIBUTING.md."
            or texts[end + 2] != "##[error]Process completed with exit code 1."):
        raise Refused("static preflight is partial or its completion boundary is unknown")
    cursor = starts[0]
    checks: set[str] = set()
    causes: set[str] = set()
    while cursor < end:
        result = CHECK_RESULT.fullmatch(texts[cursor])
        if result is None or result[2] in checks:
            raise Refused("static preflight has an unknown or duplicate check body")
        status, name = result[1], result[2]
        checks.add(name)
        cursor += 1
        if status == "PASSED":
            continue
        if name != "feature-flags" or cursor >= end or texts[cursor] != "lint-feature-flags: FAILED":
            raise Refused("static preflight has an unsupported failed-check body")
        cursor += 1
        count = 0
        while cursor < end and texts[cursor].startswith("- "):
            match = EXPIRED_FLAG.fullmatch(texts[cursor])
            if match is None or "/../" in match[1] or "/./" in match[1]:
                raise Refused("static preflight has an unknown feature-flag diagnostic")
            cause = "feature-flags: " + match[1]
            if cause in causes:
                raise Refused("static preflight has a duplicate feature-flag diagnostic")
            causes.add(cause)
            count += 1
            cursor += 1
        if (not count or cursor >= end
                or texts[cursor] != "Rerun from this checkout: python3 scripts/verify-local.py --only feature-flags"):
            raise Refused("static preflight has an incomplete or unknown feature-flag body")
        cursor += 1
    if len(checks) != int(summary[1]) or not causes or any(i >= end for i in starts):
        raise Refused("static preflight execution records do not match its complete summary")
    return causes


def crash_signals(log: str) -> set[str]:
    """The signals a test helper died on, from SwiftPM's or the hang sampler's lines."""
    return {first or second for first, second in CRASH.findall(log)}


def refused_at_setup(job: dict, log: str | None = None) -> bool:
    steps = job.get("steps") or []
    if not steps or any(step.get("name") in SETUP_STEPS and step.get("conclusion") == "failure" for step in steps):
        return True
    return log is not None and HOOK_REFUSAL in log


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("dependency context has a duplicate JSON key")
        result[key] = value
    return result


def run_metadata(repo: str, sha: str, run_id: int, github) -> dict:
    meta = github.json(f"repos/{repo}/actions/runs/{run_id}")
    if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not isinstance(meta, dict) or meta.get("id") != run_id or meta.get("head_sha") != sha
            or meta.get("status") != "completed" or meta.get("conclusion") != "failure"
            or type(meta.get("run_attempt")) is not int or meta["run_attempt"] <= 0
            or type(meta.get("workflow_id")) is not int or meta["workflow_id"] <= 0
            or any(not isinstance(meta.get(k), str) or not meta[k] for k in ("head_branch", "path", "event"))
            or not isinstance(meta.get("repository"), dict) or meta["repository"].get("full_name") != repo):
        raise Refused("job inventory needs an exact completed source/run/workflow/attempt")
    return meta


def same_run(first: dict, second: dict) -> bool:
    return all(first.get(k) == second.get(k) for k in RUN_IDENTITY)


def job_inventory(repo: str, sha: str, run_id: int, github) -> dict:
    """One complete original inventory, before selecting any failed jobs.

    The API count is not a hint: missing, duplicate or foreign rows refuse.
    Source and attempt must remain stable across collection and final audit.
    """
    meta = run_metadata(repo, sha, run_id, github)
    pages = github.json(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", paginate=True)
    if (not isinstance(pages, list) or not pages or any(not isinstance(p, dict) for p in pages)
            or any(type(p.get("total_count")) is not int or not isinstance(p.get("jobs"), list) for p in pages)
            or len({p["total_count"] for p in pages}) != 1 or not 1 <= pages[0]["total_count"] <= 5000):
        raise Refused("run has no complete bounded job inventory")
    jobs = [job for page in pages for job in page["jobs"]]
    if len(jobs) != pages[0]["total_count"]:
        raise Refused("run job inventory is partial")
    by_id, by_name = {}, {}
    for job in jobs:
        if (not isinstance(job, dict) or type(job.get("id")) is not int or job["id"] <= 0
                or job["id"] in by_id or not isinstance(job.get("name"), str) or not job["name"]
                or job["name"] in by_name or job.get("run_id") != run_id
                or job.get("head_sha") != sha or job.get("status") != "completed"):
            raise Refused("run has duplicate, incomplete or foreign job identity")
        by_id[job["id"]] = job
        by_name[job["name"]] = job
    if not same_run(meta, run_metadata(repo, sha, run_id, github)):
        raise Refused("source/run/attempt changed during job inventory collection")
    return {"meta": meta, "jobs": jobs, "by_id": by_id, "by_name": by_name}


def dependency_run(repo: str, sha: str, run_id: int, github) -> dict:
    """Read one closed CI source/initial-attempt context, without executing it.

    A new workflow source or rerun needs its own reader. The source pin binds
    the graph, display names, route predicates and program slices below.
    """
    context = job_inventory(repo, sha, run_id, github)
    meta = context["meta"]
    if meta["path"] != CI_SOURCE or meta["run_attempt"] != 1:
        raise Refused("dependency attribution needs an exact completed source/run/workflow original attempt")
    source = github.json(f"repos/{repo}/contents/{CI_SOURCE}?ref={sha}")
    if (not isinstance(source, dict) or source.get("type") != "file" or source.get("path") != CI_SOURCE
            or source.get("encoding") != "base64" or not isinstance(source.get("content"), str)):
        raise Refused("dependency workflow source is unavailable")
    raw = base64.b64decode(source["content"].replace("\n", ""), validate=True)
    if (len(raw) != source.get("size") or hashlib.sha256(raw).hexdigest() != CI_SOURCE_SHA256
            or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != source.get("sha")):
        raise Refused("dependency workflow source/graph is unknown or changed")
    texts = raw.decode().splitlines()
    programs = {}
    for name, (_, _, first, last, digest) in CI_PROGRAMS.items():
        program = "\n".join(line[10:] for line in texts[first - 1:last]).rstrip()
        if hashlib.sha256(program.encode()).hexdigest() != digest:
            raise Refused("dependency workflow program does not match its source")
        programs[name] = program.splitlines()
    changes = raw.decode().split("\n  changes:\n", 1)[1].split("\n  fast-guards:\n", 1)[0]
    output_lines = changes.split("\n    outputs:\n", 1)[1].splitlines()
    outputs = set()
    for line in output_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"      ([a-z][a-z0-9_]*):.*", line)
        if match is None:
            break
        outputs.add(match[1])
    return {**context, "programs": programs, "outputs": outputs, "rollups": {}}


def dependency_group(context: dict, name: str, result: str) -> list[dict]:
    display = CI_NAMES.get(name, name)
    exact = [j for j in context["jobs"] if j["name"] == display]
    called = [j for j in context["jobs"] if j["name"].startswith(name + " / ")]
    if name not in CI_CALLED:
        if len(exact) != 1 or called:
            raise Refused(f"{name}: missing or ambiguous logical/display job binding")
        jobs = exact
    else:
        if bool(exact) == bool(called) or len(exact) > 1:
            raise Refused(f"{name}: missing or ambiguous called-workflow job binding")
        jobs = exact or called
    states = {j.get("conclusion") for j in jobs}
    if result == "skipped":
        if not exact or states != {"skipped"} or any(j.get("steps") != [] for j in jobs):
            raise Refused(f"{name}: skipped work does not match its original job")
    elif result == "success":
        if "success" not in states or not states <= {"success", "skipped"}:
            raise Refused(f"{name}: needs verdict differs from actual jobs")
        for job in jobs:
            if job["conclusion"] == "success" and (not job.get("steps") or any(
                    step.get("status") != "completed" or step.get("conclusion") not in {"success", "skipped"}
                    for step in job["steps"])):
                raise Refused(f"{name}: successful dependency lacks completed step evidence")
    elif result == "failure":
        if "failure" not in states or not states <= {"success", "skipped", "failure"}:
            raise Refused(f"{name}: failure has no corresponding failed original job")
    else:
        raise Refused(f"{name}: cancelled, unknown or unexecuted required verdict is not attribution")
    return jobs


def dependency_needs(context: dict, needs: dict, expected) -> None:
    if not isinstance(needs, dict) or set(needs) != set(expected):
        raise Refused("dependency needs graph is missing, extra or changed")
    for name, data in needs.items():
        if (not isinstance(data, dict) or set(data) != {"result", "outputs"}
                or not isinstance(data["outputs"], dict) or (name != "changes" and data["outputs"])):
            raise Refused("dependency needs record has an unknown shape")
        dependency_group(context, name, data["result"])
    out = needs["changes"]["outputs"]
    if (needs["changes"]["result"] != "success" or not set(out) <= context["outputs"]
            or any(not isinstance(v, str) for v in out.values())
            or any(out.get(k) not in {"true", "false"} for k in CI_BOOLEAN_ROUTES)
            or ("compile_admitted" in out and out["compile_admitted"] not in {"true", "false"})
            or not re.fullmatch(r"[0-9a-f]{40}", out.get("source_parent1", ""))):
        raise Refused("dependency routes or source projection are missing, unknown or changed")
    meta = context["meta"]
    fingerprint = (r"v1;sha=" + re.escape(meta["head_sha"]) + ";ref=" + re.escape(meta["head_branch"])
                   + r";cache=(?:r2|github);release=(?:universal|arm64);coverage=(?:full|compile-only)")
    if not re.fullmatch(fingerprint, out.get("coverage_fingerprint", "")):
        raise Refused("dependency changes output belongs to a different source/ref")


def dependency_step(context: dict, name: str, github, repo: str) -> tuple[dict, list[str]]:
    job = context["by_name"].get(name)
    step, variable, *_ = CI_PROGRAMS[name]
    expected = ["Set up job", step, *(["Check routed CI jobs"] if name == "ci-status" else []), "Complete job"]
    if (job is None or job.get("conclusion") != "failure"
            or not isinstance(job.get("steps"), list) or [s.get("name") for s in job["steps"]] != expected
            or any(s.get("status") != "completed" for s in job["steps"])
            or [s.get("conclusion") for s in job["steps"]] !=
               (["success", "failure", "skipped", "success"] if name == "ci-status" else ["success", "failure", "success"])):
        raise Refused(f"{name}: failed-step or unexecuted-step ownership is unknown")
    log = github.log(repo, job)
    _, tests, errors = signature(job, log)
    if tests or errors or crash_signals(log) or refused_at_setup(job, log):
        raise Refused(f"{name}: independent error, test or runner fault is not a dependency refusal")
    rows = []
    for line in log.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[0] != name:
            raise Refused(f"{name}: log row has no exact job/step binding")
        text = LOG_COLOR.sub("", STAMP.sub("", parts[2].lstrip("\ufeff"))).rstrip()
        if parts[1] == step:
            rows.append(text)
        elif parts[1] not in {"Set up job", "Complete job"}:
            raise Refused(f"{name}: an independent or skipped step has output")
    program = context["programs"][name]
    prefix = ["##[group]Run python3 - <<'PY'", *program, "shell: /usr/bin/bash -e {0}", "env:",
              "  CI_CACHE_R2_PUBLIC_URL: https://ci-cache.cmux.com",
              '  CI_OWNED_HEAD_REPOS: ["manaflow-ai/cmux", "teamleaderleo/cmux"]']
    if rows[:len(prefix)] != prefix or len(rows) <= len(prefix) or not rows[len(prefix)].startswith("  " + variable + ": "):
        raise Refused(f"{name}: complete program/environment source evidence is missing or changed")
    rest = rows[len(prefix):]
    if rest.count("##[endgroup]") != 1:
        raise Refused(f"{name}: complete environment/body boundary is missing")
    end = rest.index("##[endgroup]")
    encoded = "\n".join([rest[0][len(variable) + 4:], *rest[1:end]])
    needs = json.loads(encoded, object_pairs_hook=unique_json)
    return needs, rest[end + 1:]


def dependency_rollup(context: dict, name: str, github, repo: str) -> dict:
    if name in context["rollups"]:
        return context["rollups"][name]
    needs, body = dependency_step(context, name, github, repo)
    dependency_needs(context, needs, LINUX_NEEDS if name == "linux-preflight" else CI_NEEDS)
    out = needs["changes"]["outputs"]
    failed = []
    if name == "linux-preflight":
        if out["macos"] != "true":
            raise Refused("linux-preflight ran outside its supported route")
        diagnostics = []
        for key in ("static-preflight", "fast-guards"):
            state = needs[key]["result"]
            if state not in {"success", "failure"}:
                raise Refused(f"{key}: unexecuted required work has no inherited failed leaf")
            if state == "failure":
                failed.append(key)
                diagnostics.append(f"{key}: failure")
        for key, routed, suffix in (
                ("guards", any(out[k] == "true" for k in ("linux_guard_tests", "linux_guard_history", "linux_guard_cli", "linux_guard_source")), "one or more guard routes=true"),
                ("web", out["web"] == "true" or out["agent_session_web"] == "true", "one or more web routes=true"),
                ("ghosttykit-release-check", out["ghosttykit_release"] == "true", "route ghosttykit_release=true")):
            state = needs[key]["result"]
            if (routed and state not in {"success", "failure"}) or (not routed and state not in {"success", "skipped"}):
                raise Refused(f"{key}: independent routing fault or unexecuted required work")
            if state == "failure":
                failed.append(key)
                diagnostics.append(f"{key}: failure ({suffix})")
    else:
        linux = dependency_rollup(context, "linux-preflight", github, repo)
        if any(needs[k] != linux["needs"][k] for k in LINUX_NEEDS):
            raise Refused("ci-status and linux-preflight disagree about the same original needs")
        if (out["macos"] != "true" or needs["linux-preflight"]["result"] != "failure"
                or needs["static-preflight"]["result"] != "failure"):
            raise Refused("ci-status has no supported early failed-static predecessor")
        # These jobs have an explicit successful-static predicate in this exact
        # source. Their absence is attributable blocking, never a passing test.
        for key in ("macos", "macos-admission-gate", "claude-wrapper", "remote-daemon", "ui-tests"):
            if needs[key]["result"] != "skipped":
                raise Refused(f"{key}: independent downstream route/result is not the supported block")
        if out["coverage_gap"] != "false" or needs["suite-coverage"]["result"] != "skipped":
            raise Refused("ci-status has an independent suite coverage fault")
        if needs["browser"]["result"] != ("success" if out["browser"] == "true" else "skipped"):
            raise Refused("ci-status has an independent browser routing fault")
        diagnostics = ["linux preflight did not pass: failure"]
        failed = ["linux-preflight"]
    if not failed or body != [*diagnostics, "##[error]Process completed with exit code 1."]:
        raise Refused(f"{name}: dependency diagnostics are partial, extra, unknown or changed")
    leaves = set()
    for key in failed:
        if key in DEPENDENCY_JOBS:
            leaves.update(dependency_rollup(context, key, github, repo)["leaves"])
        else:
            leaves.update(j["id"] for j in dependency_group(context, key, "failure") if j["conclusion"] == "failure")
    if not leaves:
        raise Refused(f"{name}: no actual failed leaf closes this dependency refusal")
    result = {"needs": needs, "failed": set(failed), "leaves": leaves}
    context["rollups"][name] = result
    return result


def head_jobs(repo: str, sha: str, runs: list[int], checks: list[str], github, inventories: dict) -> list[dict]:
    jobs: dict[int, dict] = {}

    def collect(run):
        snapshot = job_inventory(repo, sha, run, github)
        if run in inventories and (not same_run(inventories[run]["meta"], snapshot["meta"])
                                   or inventories[run]["by_id"] != snapshot["by_id"]):
            raise Refused("selected run has conflicting complete job snapshots")
        inventories[run] = snapshot
        for job in snapshot["jobs"]:
            if job.get("conclusion") not in ("success", "skipped", "neutral"):
                jobs[job["id"]] = job

    for run in runs:
        collect(run)
    if checks:
        latest = latest_checks(repo, sha, github)
        for name in checks:
            if name not in latest:
                raise Refused(f"{name}: has not run on exact head {sha}")
            job = job_for(repo, latest[name], sha, github)
            if job.get("id") != int(latest[name]["details_url"].rsplit("/", 1)[1]):
                raise Refused(f"{name}: job response is not the original required-check identity")
            collect(job["run_id"])
            listed = inventories[job["run_id"]]["by_id"].get(job["id"])
            keys = ("id", "run_id", "head_sha", "name", "status", "conclusion", "steps")
            if listed is None or any(job.get(k) != listed.get(k) for k in keys):
                raise Refused(f"{name}: required-check job differs from its complete original inventory")
    return list(jobs.values())


def judge(repo: str, base: str, sha: str, runs: list[int], github, *, checks: list[str] = ()) -> list[str]:
    """Audit lines for every excused failure; raises Refused on the first that is not."""
    audit: list[str] = []
    base_runs: dict[int, list[dict]] = {}
    base_jobs: dict[int, list[dict]] = {}
    inventories = {}
    jobs = head_jobs(repo, sha, runs, list(checks), github, inventories)
    if not jobs:
        # A red run or check with no failed job to show has nothing the base can excuse.
        raise Refused("no failed job found on the head to compare with the base")
    contexts = {}
    deferred = []
    audited = set()
    for job in jobs:
        if job.get("name") in DEPENDENCY_JOBS and job["run_id"] not in contexts:
            context = dependency_run(repo, sha, job["run_id"], github)
            selected = inventories[job["run_id"]]
            if not same_run(selected["meta"], context["meta"]) or selected["by_id"] != context["by_id"]:
                raise Refused("dependency context differs from the complete selected job snapshot")
            # Both owners must be explained, including ci-status's independent
            # routing logic; a direct --run is not a weaker entry point.
            dependency_rollup(context, "ci-status", github, repo)
            contexts[job["run_id"]] = context
    for job in jobs:
        name = job.get("name", "")
        if job.get("conclusion") != "failure":
            raise Refused(f"'{name}' is {job.get('conclusion')}: it has no verdict to compare with the base; re-run it")
        if refused_at_setup(job):
            raise Refused(f"'{name}' was refused at setup by its runner; it gets re-run, never excused")
        head_log = github.log(repo, job)
        if refused_at_setup(job, head_log):
            raise Refused(f"'{name}' was refused at setup by its runner; it gets re-run, never excused")
        workflow = (contexts[job["run_id"]]["meta"]["workflow_id"] if job["run_id"] in contexts
                    else github.json(f"repos/{repo}/actions/runs/{job['run_id']}").get("workflow_id"))
        if workflow not in base_runs:
            listed = github.json(f"repos/{repo}/actions/workflows/{workflow}/runs?branch={base}&status=completed&per_page=20")
            base_runs[workflow] = [run for run in listed.get("workflow_runs") or []
                                   if run.get("event") != "pull_request" and run.get("conclusion") != "skipped"]
        # The newest base run where this job has a verdict decides: a base push
        # run's Mac job is often cancelled by the next push, or refused by a mini.
        base_run = base_job = None
        for candidate_run in base_runs[workflow]:
            if candidate_run["id"] not in base_jobs:
                base_jobs[candidate_run["id"]] = [found for page in github.json(
                    f"repos/{repo}/actions/runs/{candidate_run['id']}/jobs?filter=latest&per_page=100", paginate=True)
                    for found in page.get("jobs") or []]
            found = next((found for found in base_jobs[candidate_run["id"]] if found.get("name") == name), None)
            if found is not None and found.get("conclusion") in VERDICTS and not refused_at_setup(found):
                base_run, base_job = candidate_run, found
                break
        if base_run is None:
            raise Refused(f"'{name}': no completed {base} run of its workflow has a verdict for it to compare with")
        if base_job.get("conclusion") != "failure":
            raise Refused(f"'{name}' is not red on {base} run {base_run['id']} ({base_job.get('conclusion')}); "
                          "fix it on this head")
        if name in DEPENDENCY_JOBS:
            head_context = contexts[job["run_id"]]
            if head_context["by_id"].get(job["id"]) != job:
                raise Refused(f"{name}: original job changed during dependency collection")
            if base_run["id"] not in contexts:
                context = dependency_run(repo, base_run.get("head_sha"), base_run["id"], github)
                if (context["meta"]["workflow_id"] != workflow or context["meta"]["head_branch"] != base
                        or base_run.get("workflow_id") != workflow):
                    raise Refused(f"{name}: base source/workflow/branch differs")
                dependency_rollup(context, "ci-status", github, repo)
                if {j["id"]: j for j in base_jobs[base_run["id"]]} != context["by_id"]:
                    raise Refused("base dependency context differs from its original job snapshot")
                contexts[base_run["id"]] = context
            base_context = contexts[base_run["id"]]
            if base_context["by_id"].get(base_job["id"]) != base_job:
                raise Refused(f"{name}: base job changed during dependency collection")
            head_rollup = dependency_rollup(head_context, name, github, repo)
            base_rollup = dependency_rollup(base_context, name, github, repo)
            if (head_rollup["failed"] - base_rollup["failed"]
                    or head_rollup["needs"]["changes"]["outputs"]["source_parent1"] != base_context["meta"]["head_sha"]):
                raise Refused(f"{name}: dependency/source cause is not on the compared base original")
            deferred.append((job, base_run, head_rollup))
            continue
        head_steps, head_tests, head_errors = signature(job, head_log)
        head_crash = crash_signals(head_log)
        head_causes = static_causes(job, head_log)
        if not head_tests and not head_errors and not head_crash and not head_causes:
            raise Refused(f"'{name}': no failing test or error line to match; nothing to compare with the base")
        base_log = github.log(repo, base_job)
        base_steps, base_tests, base_errors = signature(base_job, base_log)
        base_causes = static_causes(base_job, base_log)
        if head_causes and (job.get("head_sha") != sha
                            or base_run.get("head_branch") != base
                            or base_run.get("workflow_id") != workflow
                            or not re.fullmatch(r"[0-9a-f]{40}", base_run.get("head_sha") or "")
                            or base_job.get("head_sha") != base_run["head_sha"]
                            or base_job.get("run_id") != base_run["id"]):
            raise Refused(f"'{name}': structured diagnostics have a different source, workflow or job binding")
        if missing := sorted(head_crash - crash_signals(base_log)):
            raise Refused(f"'{name}': the test helper died on signal {', '.join(missing)}, "
                          f"and {base} run {base_run['id']} did not")
        compared = [("failed steps", head_steps, base_steps), ("failing tests", head_tests, base_tests)]
        compared.append(("static check causes", head_causes, base_causes))
        if not head_tests or head_causes:
            compared.append(("errors", head_errors, base_errors))
        for kind, head, seen in compared:
            if missing := sorted(head - seen):
                raise Refused(f"'{name}': {kind} not on {base} run {base_run['id']}: {'; '.join(missing)[:600]}")
        crash = [f"the test helper died on signal {signal}" for signal in sorted(head_crash)]
        what = "; ".join(crash + sorted(head_tests) + sorted(head_causes)) or "; ".join(sorted(head_errors))
        url = base_run.get("html_url") or f"https://github.com/{repo}/actions/runs/{base_run['id']}"
        audit.append(f"excused '{name}': {base} run {base_run['id']} fails the same way ({url}): {what[:400]}")
        audited.add(job["id"])
    for job, base_run, rollup in deferred:
        if not rollup["leaves"] <= audited:
            raise Refused(f"{job['name']}: not every failed dependency closed through the ordinary base leaf audit")
        skipped = sorted(k for k, v in rollup["needs"].items() if v["result"] == "skipped")
        audit.append(f"attributed '{job['name']}': exact dependency refusal on {base} run {base_run['id']}; "
                     f"failed leaves independently audited; skipped work unexecuted: {', '.join(skipped) or 'none'}; "
                     + ("later platform assertions and Check routed CI jobs unexecuted" if job["name"] == "ci-status"
                        else "no skipped work is called passing"))
    for run_id, context in {**inventories, **contexts}.items():
        current = run_metadata(repo, context["meta"]["head_sha"], run_id, github)
        if not same_run(current, context["meta"]):
            raise Refused("run/attempt changed during the original audit")
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run", type=int, action="append", default=[], help="a failed workflow run on the head")
    parser.add_argument("--check", action="append", default=[], help="a red required check on the head")
    args = parser.parse_args(argv)
    try:
        audit = judge(args.repo, args.base, args.sha, args.run, GitHub(), checks=args.check)
    except (Refused, KeyError, ValueError) as error:
        print(f"not excused: {error}", file=sys.stderr)
        return 1
    print("\n".join(audit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
