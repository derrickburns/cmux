#!/usr/bin/env python3
"""The owned-pool rescue watches every workflow that can take an owned pool, on main and on feat-cmux-next.

ci-owned-pool-rescue.yml and scripts/ci/owned_pool_rescue.py run from main for
both branches, and the rescue acts only on the workflow files it lists
(owned_pool_rescue.WATCHED_WORKFLOW_PATHS). Run 39638669325
(cmux-next-base-autofix.yml, a feat-cmux-next push, 2026-10-21) had its Mac job
refused by a mini at job start, and nothing re-ran it: the workflow exists only
on feat-cmux-next and main's list did not name it.

scripts/ci/owned_pool_sources.py finds the owned workflows from their jobs'
runs-on. This test reads them from the working tree and from the other branch
(origin/feat-cmux-next on main, origin/main on feat-cmux-next), and the list
from main's copy of the rescue (the working tree on main, origin/main on
feat-cmux-next, since that is the copy that runs). It fails when an owned
workflow of either branch is not listed, and when a listed one is owned in
neither (only a warning for a workflow main does not have: main's list cannot
change in the commit that moves a feat-cmux-next-only workflow off the minis,
and a stale entry is harmless). The other branch is read with `git show`; under GitHub Actions the
test fetches it (one shallow, blob-less fetch, then the files it reads) and
fails if it still cannot read it. Off CI a missing ref only skips that branch.
An organization checkout keeps that origin route. A verified personal fork
instead reads an exact official counterpart under an upstream-named Git ref;
only bounded workflow and detector-input blobs are hydrated, never executed.

Which branch the tree is: CMUX_OWNED_POOL_BRANCH when set; under Actions the
branch the event targets (GITHUB_BASE_REF for a pull request, else
GITHUB_REF_NAME, a merge queue's gh-readonly-queue/<branch>/... included, and
main for any other branch); off CI the branch with fewer commits between it and
HEAD, skipped when either ref is missing. (Both branches carry a cmux-next.yml,
so no file tells them apart.)
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from git_fixture_env import without_auto_maintenance

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import owned_pool_sources as sources  # noqa: E402

FEAT = "feat-cmux-next"
BRANCHES = {"main": "origin/main", FEAT: "origin/feat-cmux-next"}
ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def git(*args: str, stdin: str | None = None, no_lazy: bool = False) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"} if no_lazy else None
    return subprocess.run(["git", "-C", str(ROOT), *args], input=stdin, capture_output=True, text=True, env=env)


def branch() -> str | None:
    """main or feat-cmux-next: the branch this tree is (or is going into), or None when it cannot be told."""
    named = os.environ.get("CMUX_OWNED_POOL_BRANCH")
    if named:
        return named
    if ON_CI:
        ref = os.environ.get("GITHUB_BASE_REF") or os.environ.get("GITHUB_REF_NAME") or ""
        if ref.startswith("gh-readonly-queue/"):
            ref = ref[len("gh-readonly-queue/"):].rsplit("/pr-", 1)[0]
        if ref in BRANCHES:
            return ref
        # A dispatch on a topic branch: judged as main's, the rescue's home.
        print(f"owned pool sources: {ref or 'no ref'} is neither branch; checking it as main", file=sys.stderr)
        return "main"
    ahead = {}
    for name, ref in BRANCHES.items():
        counted = git("rev-list", "--count", f"{ref}..HEAD")
        if counted.returncode != 0:
            return None
        ahead[name] = int(counted.stdout.strip() or 0)
    return min(ahead, key=ahead.get)


def on_feat() -> bool:
    return branch() == FEAT


def other_ref() -> str:
    return BRANCHES["main"] if on_feat() else BRANCHES[FEAT]


def has_ref(ref: str) -> bool:
    return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def fetch(ref: str) -> None:
    """The other branch's tip with no blobs, then the blobs of the files the detector reads, in two requests."""
    name = next((name for name, known in BRANCHES.items() if known == ref), None)
    if name is None:
        return
    git("fetch", "--no-tags", "--depth=1", "--filter=blob:none", "origin",
        f"+refs/heads/{name}:refs/remotes/{ref}")
    listing = git("ls-tree", ref, f"{sources.WORKFLOWS_DIR}/", "scripts/ci/")
    blobs = [line.split()[2] for line in listing.stdout.splitlines() if line.split()[1:2] == ["blob"]]
    if blobs:
        git("-c", "fetch.negotiationAlgorithm=noop", "fetch", "--no-tags", "--filter=blob:none", "--stdin",
            "origin", stdin="\n".join(blobs) + "\n")


UPSTREAM = "manaflow-ai/cmux"
UPSTREAM_ID = 1144115288
UPSTREAM_URL = f"https://github.com/{UPSTREAM}.git"
COMPARISON_MAX_FILES = 1024
COMPARISON_MAX_BYTES = 16 * 1024 * 1024
COMPARISON_MAX_FILE_BYTES = 1024 * 1024
API_MAX_BYTES = 2 * 1024 * 1024
OID = re.compile(r"[0-9a-f]{40}")


class ComparisonSourceError(RuntimeError):
    def __init__(self, message: str):
        super().__init__("comparison source: " + message)


def comparison_api(path: str):
    """Bounded public source metadata, using the existing read token when supplied."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    # The workflow token belongs to its current repository. The official
    # counterpart is public data, not an assumed installation permission.
    requested_repository = "/".join(path.split("/", 2)[:2])
    if token and requested_repository.lower() == os.environ.get("GITHUB_REPOSITORY", "").lower():
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/repos/" + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(API_MAX_BYTES + 1)
        if len(raw) > API_MAX_BYTES:
            raise ComparisonSourceError("metadata response exceeds its byte budget")
        return json.loads(raw)
    except (OSError, ValueError) as error:
        raise ComparisonSourceError("public source metadata is unavailable or malformed") from error


def comparison_git(*args: str, stdin: str | None = None) -> str:
    try:
        result = git(*args, stdin=stdin, no_lazy=True)
    except (OSError, UnicodeError) as error:
        raise ComparisonSourceError("Git source is unavailable or not UTF-8") from error
    if result.returncode:
        raise ComparisonSourceError("Git source read/fetch failed: " + " ".join(result.stderr.split())[-300:])
    return result.stdout


def comparison_path(path: str) -> bool:
    parent, _, name = path.rpartition("/")
    return parent == sources.WORKFLOWS_DIR and name.endswith((".yml", ".yaml")) \
        or parent == "scripts/ci" and name.endswith(".py")


def comparison_inventory(sha: str) -> dict[str, tuple[str, int]]:
    files = {}
    for directory in (sources.WORKFLOWS_DIR, "scripts/ci"):
        rows = comparison_api(f"{UPSTREAM}/contents/{directory}?ref={sha}")
        # GitHub's directory endpoint is limited to 1,000 entries. Do not
        # mistake a potentially capped response for a complete source list.
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise ComparisonSourceError("source directory is unavailable or exceeds the entry budget")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise ComparisonSourceError("invalid source directory entry")
            path = row["path"]
            if not comparison_path(path):
                continue
            name = path.rsplit("/", 1)[-1]
            if path != directory + "/" + name or row.get("name") != name \
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or path in seen:
                raise ComparisonSourceError("source path is conflicting or unsupported")
            seen.add(path)
            oid, size = row.get("sha"), row.get("size")
            if row.get("type") != "file" or row.get("target") or row.get("submodule_git_url") \
                    or not isinstance(oid, str) or not OID.fullmatch(oid) \
                    or type(size) is not int or not 0 <= size <= COMPARISON_MAX_FILE_BYTES:
                raise ComparisonSourceError("source file metadata exceeds its supported boundary")
            files[path] = (oid, size)
    required = {sources.POOL_SCRIPT, sources.POLICY_SCRIPT, sources.RESCUE_SCRIPT}
    if not required <= files.keys() or not any(path.startswith(sources.WORKFLOWS_DIR + "/") for path in files):
        raise ComparisonSourceError("required workflow/policy source is missing")
    objects = {}
    for oid, size in files.values():
        if oid in objects and objects[oid] != size:
            raise ComparisonSourceError("one source object has conflicting sizes")
        objects[oid] = size
    if len(files) > COMPARISON_MAX_FILES or sum(objects.values()) > COMPARISON_MAX_BYTES:
        raise ComparisonSourceError("required CI source exceeds its hydration budget")
    return files


def hydrate_comparison(files: dict[str, tuple[str, int]]) -> None:
    oids = sorted({oid for oid, _ in files.values()})
    if not oids:
        return
    comparison_git("-c", "fetch.negotiationAlgorithm=noop", "fetch", "--no-tags", "--no-write-fetch-head",
                   "--filter=blob:none", "--stdin", UPSTREAM_URL, stdin="\n".join(oids) + "\n")
    actual = comparison_git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                            stdin="\n".join(oids) + "\n").splitlines()
    expected = {f"{oid} blob {size}" for oid, size in files.values()}
    if len(actual) != len(oids) or set(actual) != expected:
        raise ComparisonSourceError("hydrated source bytes do not match their declared objects/sizes")


def comparison_bytes(files: dict[str, tuple[str, int]]) -> int:
    return sum({oid: size for oid, size in files.values()}.values())


def ci_comparison_ref(ref: str) -> tuple[str, str | None]:
    """Keep organization/origin behavior; a personal fork reads a pinned official counterpart."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise ComparisonSourceError("repository context is missing or invalid")
    if repository.split("/", 1)[0].lower() == "manaflow-ai":
        return ref, None
    name = next((name for name, known in BRANCHES.items() if known == ref), None)
    if name is None:
        raise ComparisonSourceError("unknown counterpart branch")
    origin = comparison_git("remote", "get-url", "origin").strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                         r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", origin)
    if not match or match[1].lower() != repository.lower():
        raise ComparisonSourceError("origin does not match the repository context")
    metadata = comparison_api(repository)
    if not isinstance(metadata, dict):
        raise ComparisonSourceError("invalid repository metadata")
    parent, owner = metadata.get("parent"), metadata.get("owner")
    full_name = metadata.get("full_name")
    if type(metadata.get("id")) is not int or metadata["id"] <= 0 \
            or str(metadata["id"]) != os.environ.get("GITHUB_REPOSITORY_ID") \
            or not isinstance(full_name, str) or full_name.lower() != repository.lower() \
            or metadata.get("fork") is not True \
            or not isinstance(owner, dict) or owner.get("type") != "User" \
            or not isinstance(owner.get("login"), str) \
            or owner["login"].lower() != repository.split("/", 1)[0].lower() \
            or not isinstance(parent, dict) or type(parent.get("id")) is not int \
            or parent["id"] != UPSTREAM_ID or parent.get("full_name") != UPSTREAM:
        raise ComparisonSourceError("repository is not the declared personal fork of the official parent")
    branch = comparison_api(f"{UPSTREAM}/git/ref/heads/{name}")
    obj = branch.get("object") if isinstance(branch, dict) else None
    if not isinstance(obj, dict) or branch.get("ref") != f"refs/heads/{name}" \
            or obj.get("type") != "commit" or not isinstance(obj.get("sha"), str) or not OID.fullmatch(obj["sha"]):
        raise ComparisonSourceError("official branch/ref/commit identity differs")
    sha = obj["sha"]
    files = comparison_inventory(sha)
    namespace = f"refs/cmux-ci/upstream/{UPSTREAM}/{name}/{sha}"
    print("owned pool comparison: " + json.dumps({"phase": "planned_before_fetch", "repository": repository,
          "repository_id": metadata["id"], "parent": UPSTREAM, "parent_id": UPSTREAM_ID,
          "branch": name, "sha": sha, "namespace": namespace, "candidate_files": len(files),
          "candidate_bytes": comparison_bytes(files)}),
          file=sys.stderr)
    cached = git("rev-parse", "--verify", "--quiet", namespace, no_lazy=True)
    if cached.returncode == 0 and cached.stdout.strip() != sha:
        raise ComparisonSourceError("cached official namespace has a different commit")
    if cached.returncode:
        comparison_git("fetch", "--no-tags", "--no-write-fetch-head", "--depth=1", "--filter=blob:none",
                       UPSTREAM_URL, f"{sha}:{namespace}")
    if comparison_git("rev-parse", f"{namespace}^{{commit}}").strip() != sha:
        raise ComparisonSourceError("fetched comparison commit differs from the selected source")
    listed = {}
    for row in comparison_git("ls-tree", "-z", sha, sources.WORKFLOWS_DIR + "/", "scripts/ci/").split("\0"):
        if not row:
            continue
        header, path = row.split("\t", 1)
        mode, kind, oid = header.split()
        if comparison_path(path):
            if mode not in ("100644", "100755") or kind != "blob":
                raise ComparisonSourceError("comparison includes a nonregular source file")
            listed[path] = oid
    if listed != {path: oid for path, (oid, _) in files.items()}:
        raise ComparisonSourceError("Git tree and pre-fetch source inventory differ")
    workflows = {path: row for path, row in files.items() if path.startswith(sources.WORKFLOWS_DIR + "/")}
    hydrate_comparison(workflows)
    reads = {sources.POOL_SCRIPT, sources.POLICY_SCRIPT, sources.RESCUE_SCRIPT}
    for path in workflows:
        reads.update(sources.SCRIPT_PATH.findall(comparison_git("show", f"{sha}:{path}")))
    # Absent script references retain the Tree reader's ordinary None. The
    # complete direct CI tree above proves absence; do not synthesize a file.
    scripts = {path: files[path] for path in reads if path in files and path not in workflows}
    hydrate_comparison(scripts)
    if comparison_git("rev-parse", f"{namespace}^{{commit}}").strip() != sha:
        raise ComparisonSourceError("official namespace changed before adoption")
    print("owned pool comparison: " + json.dumps({"phase": "verified", "namespace": namespace, "sha": sha,
          "hydrated_files": len(workflows) + len(scripts),
          "hydrated_bytes": comparison_bytes({**workflows, **scripts})}),
          file=sys.stderr)
    return namespace, sha


class OwnedPoolSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.branch = branch()
        if cls.branch is None:
            raise unittest.SkipTest("cannot tell whether this tree is main or feat-cmux-next (a ref is missing); "
                                    "set CMUX_OWNED_POOL_BRANCH")
        cls.ref = other_ref()
        comparison_sha = None
        if ON_CI:
            cls.ref, comparison_sha = ci_comparison_ref(cls.ref)
        if not has_ref(cls.ref) and ON_CI:
            fetch(cls.ref)
        # The namespace retains explicit owner provenance; immutable bytes
        # remain pinned even if another process later moves that local ref.
        cls.other = sources.Tree(ROOT, comparison_sha or cls.ref) if has_ref(cls.ref) else None
        here = sources.Tree(ROOT)
        cls.trees = [here] + ([cls.other] if cls.other is not None else [])
        rescue_tree = cls.rescue_tree = cls.other if on_feat() else here
        source = rescue_tree.read(sources.RESCUE_SCRIPT) if rescue_tree is not None else None
        cls.watched = sources.watched_paths(source) if source is not None else None
        cls.unrescued = sources.unrescued_paths(source) if source is not None else frozenset()
        # Either copy may acknowledge an unrescued workflow (feat-cmux-next #30867 accepts main's on its side). An
        # unrescued entry changes no behaviour, since the rescue acts only on watched paths, and watched paths
        # still come from main's copy, the one that runs. feat-cmux-next's copy lists the workflows that read
        # vars.RUNNER_POOLS.linux there (feat-cmux-next #30878).
        for tree in cls.trees:
            own = tree.read(sources.RESCUE_SCRIPT)
            if own is not None and sources.UNRESCUED_NAME in own:
                cls.unrescued = cls.unrescued | sources.unrescued_paths(own)
        cls.owned = {str(tree): sources.owned_workflows(tree) for tree in cls.trees}

    def test_the_other_branch_is_read(self) -> None:
        if self.other is None:
            message = f"{self.ref} is not available, so its workflows were not checked"
            if ON_CI:
                self.fail(f"{message}; the guard job must be able to fetch it")
            self.skipTest(message)

    def test_every_owned_workflow_is_watched(self) -> None:
        if self.watched is None:
            self.skipTest(f"main's {sources.RESCUE_SCRIPT} is not readable here ({self.ref} missing)")
        for tree, owned in self.owned.items():
            with self.subTest(tree=tree):
                missing = {path: why for path, why in owned.items()
                           if path not in self.watched and path not in self.unrescued}
                self.assertFalse(missing, "\n".join(
                    [f"{tree}: these workflows can take an owned pool but main's {sources.RESCUE_SCRIPT} does not "
                     "watch them, so a refused or stuck job of theirs is never moved; add each to "
                     "SIDE_WORKFLOW_PATHS on main (a feat-cmux-next-only one too), or to UNRESCUED_WORKFLOW_PATHS "
                     "with why a person re-runs it:"]
                    + [f"  {path}: {why}" for path, why in sorted(missing.items())]))

    def test_every_nightly_sign_verify_and_dmg_job_on_blacksmith_macos_is_rescued(self) -> None:
        # Run 40585960129 (2026-10-28): nightly-next's signer-verify sat on blacksmith-6vcpu-macos-26 for hours, and
        # being outside the rescue's table it also held the run's stalled final-DMG verify. The jobs are read from
        # each branch's nightly.yml, never listed here, so a new or renamed one fails until the rescue covers it.
        source = self.rescue_tree.read(sources.RESCUE_SCRIPT) if self.rescue_tree is not None else None
        if source is None:
            self.skipTest(f"main's {sources.RESCUE_SCRIPT} is not readable here ({self.ref} missing)")
        table = sources.sign_table(source)
        for tree in self.trees:
            text = tree.read(sources.NIGHTLY_WORKFLOW)
            if text is None:
                continue
            with self.subTest(tree=str(tree)):
                jobs = sources.sign_jobs(text)
                self.assertTrue(jobs, f"{tree}: no sign or verify job found; the detector no longer matches nightly.yml")
                # main's nightly still signs on Blacksmith macOS, so an empty candidate list there means the runs-on
                # detector stopped matching. feat-cmux-next's nightly runs only on owned Macs (d5997935ef7), so it
                # may have none, but only while none of its sign jobs names Blacksmith or a MACOS_RUNNER variable.
                hinted = {job: runs_on for job, runs_on in jobs.items()
                          if "blacksmith" in sources.OUTAGE_MACOS_FALLBACK.sub("", runs_on).lower()
                          or "MACOS_RUNNER" in runs_on}
                if tree is self.rescue_tree or hinted:
                    self.assertTrue(sources.sign_stall_candidates(text), "\n".join(
                        [f"{tree}: no sign or verify job on a Blacksmith macOS label found; the detector no longer "
                         "matches nightly.yml:"] + [f"  {job}: {runs_on}" for job, runs_on in sorted(hinted.items())]))
                missing = sources.uncovered_sign_jobs(text, table)
                self.assertFalse(missing, "\n".join(
                    [f"{tree}: these nightly.yml jobs can queue on a Blacksmith macOS label, but main's "
                     f"{sources.RESCUE_SCRIPT} cannot move them off a stalled one; add each to "
                     f"{sources.SIGN_TABLE_NAME} with the runs-on expression that gives its re-run another label:"]
                    + [f"  {job}: {why}" for job, why in sorted(missing.items())]))

    def test_the_sign_stall_detector(self) -> None:
        table = {"signer-verify": ("needs.pick.outputs.alternate",)}
        workflow = textwrap.dedent("""
            jobs:
              signer-verify:
                runs-on: ${{ github.run_attempt > 1 && needs.pick.outputs.alternate || 'blacksmith-6vcpu-macos-26' }}
              signer-dmg-image:
                runs-on: ${{ vars.MACOS_RUNNER_26 || 'macos-26' }}
              verify-final-dmg-nightly:
                runs-on: ${{ needs.pick.outputs.runner || 'blacksmith-12vcpu-macos-26' }}
              pick-nightly-sign-runner:
                runs-on: ${{ vars.CI_CLOUD_OVERFLOW_SAVED && 'blacksmith-6vcpu-macos-26' || 'blacksmith-4vcpu-ubuntu-2404' }}
              build-nightly-app:
                runs-on: blacksmith-6vcpu-macos-26
              signer-build:
                runs-on: blacksmith-8vcpu-ubuntu-2404
        """)
        self.assertEqual(sorted(sources.sign_stall_candidates(workflow)),
                         ["signer-dmg-image", "signer-verify", "verify-final-dmg-nightly"])
        self.assertEqual(sorted(sources.sign_jobs(workflow)), [
            "pick-nightly-sign-runner", "signer-build", "signer-dmg-image", "signer-verify", "verify-final-dmg-nightly"])
        owned_only = textwrap.dedent("""
            jobs:
              build-sign-notarize-nightly:
                runs-on: ${{ needs.pick-nightly-sign-runner.outputs.sign_owned || vars.CI_NIGHTLY_NEXT_BUILD_RUNNER }}
              verify-signed-nightly:
                runs-on: ${{ needs.pick-nightly-sign-runner.outputs.owned || 'glaeda-std-xcode-26.6' }}
        """)
        self.assertEqual(sorted(sources.sign_jobs(owned_only)), ["build-sign-notarize-nightly", "verify-signed-nightly"])
        self.assertEqual(sources.sign_stall_candidates(owned_only), {})
        self.assertEqual(sources.uncovered_sign_jobs(owned_only, {}), {})
        self.assertEqual(sources.uncovered_sign_jobs(workflow, {**table, "verify-final-dmg-nightly": ("x",)}), {
            "signer-dmg-image": f"not in {sources.SIGN_TABLE_NAME}",
            "verify-final-dmg-nightly": "its runs-on takes none of its alternate-label expressions on a re-run"})
        self.assertEqual(sources.sign_table(
            'A = "x"\nB = ("y "\n     "z")\nNIGHTLY_SIGN_ALTERNATES: dict = {"j": ("J", (A, B)), "k": ("K", (A,))}\n'),
            {"j": ("x", "y z"), "k": ("x",)})

    def test_portable_light_placement_jobs_can_run_on_attempt_three(self) -> None:
        source = self.rescue_tree.read(sources.RESCUE_SCRIPT) if self.rescue_tree is not None else None
        if source is None:
            self.skipTest("main's rescue is not readable")
        paths = sources._constants(source).get("PORTABLE_LIGHT_WORKFLOW_PATHS", ())
        self.assertEqual(len(paths), 3)
        seen = set()
        for tree in self.trees:
            for path in paths:
                text = tree.read(path)
                if text is None:
                    continue
                placement = sources._load(text).get("jobs", {}).get("portable-placement")
                if not isinstance(placement, dict):
                    continue  # Older main copies have no placement graph and fail closed.
                with self.subTest(tree=str(tree), workflow=path):
                    self.assertNotIn("github.run_attempt", str(placement.get("if") or ""),
                                     "attempt 2 rescue must not skip placement on attempt 3")
                    self.assertTrue(any("portable_job_placement.py" in str(step.get("run") or "")
                                        for step in placement.get("steps", [])))
                    seen.add(path)
        if self.other is not None:
            self.assertEqual(seen, set(paths), "all three deployed portable graphs must be checked")

    def test_every_watched_workflow_is_owned_on_some_branch(self) -> None:
        if self.watched is None or self.other is None:
            self.skipTest("both branches are needed to call an entry stale")
        found = set().union(*(set(owned) for owned in self.owned.values()))
        stale = (self.watched | self.unrescued) - found
        # A feat-cmux-next-only workflow moved off the minis leaves its entry in main's list, which no
        # commit can change together with it (the entry cannot go first either: main's own run would
        # find the workflow owned and unwatched). A stale entry only watches a run with no owned job.
        on_main = set(self.rescue_tree.workflows())
        feat_only = sorted(stale - on_main)
        if feat_only:
            print(f"::warning title=owned pool sources::{sources.RESCUE_SCRIPT} on main lists "
                  f"feat-cmux-next-only workflows that take no owned pool any more; remove them in a pull "
                  f"request to main: {feat_only}", file=sys.stderr)
        # An UNRESCUED entry makes the rescue do nothing, so a stale one changes no behaviour, and it is how a
        # workflow that only feat-cmux-next is about to make owned gets named on main first: main's list must
        # name it before that pull request's guard can pass, while neither branch owns it yet (release.yml's
        # signing picker, cmux#27066). So only a stale WATCHED entry fails; a stale unrescued one warns.
        waiting = sorted((stale & on_main) & set(self.unrescued))
        if waiting:
            print(f"::warning title=owned pool sources::{sources.RESCUE_SCRIPT} on main leaves to a person "
                  f"workflows that take no owned pool yet: {waiting}; remove any that will not", file=sys.stderr)
        self.assertFalse(sorted((stale & on_main) - set(self.unrescued)),
                         f"{sources.RESCUE_SCRIPT} watches workflows that take no owned pool on main or "
                         f"feat-cmux-next any more; remove them")
        self.assertFalse(self.watched & self.unrescued, "a workflow is both watched and left to a person")

    def test_the_markers_come_from_the_pool_code(self) -> None:
        # Both branches' policies: feat-cmux-next's drifted_runner_variables() also names LINUX_RUNNER and the
        # hosted-Mac fallbacks in a no-hosted-Mac guard, which once made ~130 workflows look owned on every PR.
        for tree in self.trees:
            with self.subTest(tree=str(tree)):
                markers = sources.Markers(tree)
                self.assertEqual(markers.literal, "glaeda-")
                for variable in ("CI_PR_POOL_OWNED", "CI_SIDE_LANE_RUNNER", "CI_LIGHT_LANE_RUNNER",
                                 "CI_SEED_TRUSTED_POOL", "CI_NIGHTLY_TRUSTED_RUNNER"):
                    self.assertIn(variable, markers.variables)
                for hosted in ("LINUX_RUNNER", "MACOS_RUNNER_15", "MACOS_RUNNER_26", "NIGHTLY_NEXT_TRAIN_RUNNER"):
                    self.assertNotIn(hosted, markers.variables)

    def test_a_runs_on_is_owned_by_a_label_a_pool_variable_or_a_picker_output(self) -> None:
        markers = sources.Markers(sources.Tree(ROOT))
        owned = (
            "${{ vars.CI_PR_POOL_OWNED == '1' && 'glaeda-session-std-xcode-26.6' || 'blacksmith-6vcpu-macos-26' }}",
            "${{ github.run_attempt <= 2 && vars.CI_SIDE_LANE_RUNNER || 'blacksmith-6vcpu-macos-26' }}",
            "${{ fromJSON(needs.runner.outputs.runs_on) }}",
        )
        for runs_on in owned:
            self.assertTrue(markers.owned_runs_on(runs_on, {"runner"}), runs_on)
        for runs_on in ("blacksmith-4vcpu-ubuntu-2404", "${{ vars.LINUX_RUNNER || 'ubuntu-24.04' }}",
                        "${{ vars.MACOS_RUNNER_26 || 'macos-26' }}", "${{ needs.plan.outputs.runner }}"):
            self.assertFalse(markers.owned_runs_on(runs_on, {"runner"}), runs_on)

    def scratch_tree(self, workflows: dict[str, str]) -> sources.Tree:
        """A tree holding these workflows and this checkout's pool code (the markers' source)."""
        root = Path(tempfile.mkdtemp(prefix="owned-pool-sources-"))
        self.addCleanup(shutil.rmtree, root, True)
        for path in (sources.POOL_SCRIPT, sources.POLICY_SCRIPT):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / path, root / path)
        for name, text in workflows.items():
            (root / sources.WORKFLOWS_DIR).mkdir(parents=True, exist_ok=True)
            (root / sources.WORKFLOWS_DIR / name).write_text(textwrap.dedent(text), encoding="utf-8")
        return sources.Tree(root)

    def test_a_runs_on_is_followed_into_what_it_reads(self) -> None:
        owned = sources.owned_workflows(self.scratch_tree({
            # seed-derived-data.yml: the matrix comes from a job whose env reads the trusted pool variable.
            "matrix.yml": """
                on: push
                jobs:
                  decide:
                    runs-on: ubuntu-24.04
                    outputs:
                      matrix: ${{ steps.pick.outputs.matrix }}
                    steps:
                      - id: pick
                        env:
                          POOL: ${{ vars.CI_SEED_TRUSTED_POOL }}
                        run: echo "matrix=[\"$POOL\"]" >> "$GITHUB_OUTPUT"
                  seed:
                    needs: decide
                    strategy:
                      matrix: ${{ fromJSON(needs.decide.outputs.matrix) }}
                    runs-on: ${{ matrix.pool }}
                    steps:
                      - run: true
            """,
            # main-compile-probe.yml: a dispatch input whose default is an owned label.
            "input.yml": """
                on:
                  workflow_dispatch:
                    inputs:
                      runner:
                        default: glaeda-root-std-xcode-26.6
                jobs:
                  compile:
                    runs-on: ${{ inputs.runner }}
                    steps:
                      - run: true
            """,
            # A reusable workflow whose runs-on is an input with no owned default, and its two callers.
            "reusable.yml": """
                on:
                  workflow_call:
                    inputs:
                      runner:
                        type: string
                        default: ubuntu-24.04
                jobs:
                  build:
                    runs-on: ${{ inputs.runner }}
                    steps:
                      - run: true
            """,
            "caller.yml": """
                on: push
                jobs:
                  build:
                    uses: ./.github/workflows/reusable.yml
                    with:
                      runner: glaeda-side-light-xcode-26.6
            """,
            "plain-caller.yml": """
                on: push
                jobs:
                  build:
                    uses: ./.github/workflows/reusable.yml
                    with:
                      runner: blacksmith-6vcpu-macos-26
            """,
            "plain.yml": """
                on: push
                jobs:
                  decide:
                    runs-on: ubuntu-24.04
                    outputs:
                      runner: ${{ steps.pick.outputs.runner }}
                    steps:
                      - id: pick
                        env:
                          RUNNER: ${{ vars.MACOS_RUNNER_26 }}
                        run: echo "runner=$RUNNER" >> "$GITHUB_OUTPUT"
                  build:
                    needs: decide
                    runs-on: ${{ needs.decide.outputs.runner }}
                    steps:
                      - run: true
            """,
        }))
        paths = {path.rsplit("/", 1)[-1]: why for path, why in owned.items()}
        self.assertEqual(sorted(paths), ["caller.yml", "input.yml", "matrix.yml"], owned)
        self.assertIn("vars.CI_SEED_TRUSTED_POOL", paths["matrix.yml"])
        self.assertIn("input runner", paths["input.yml"])
        self.assertIn("reusable.yml", paths["caller.yml"])

    def test_run_39638669325s_workflow_is_watched(self) -> None:
        # The base autofix's Mac exports take glaeda-session-std-xcode-26.6 on feat-cmux-next.
        if self.watched is None:
            self.skipTest("main's rescue is not readable here")
        self.assertIn(".github/workflows/cmux-next-base-autofix.yml", self.watched)
        feat = sources.Tree(ROOT) if on_feat() else self.other if self.branch == "main" else None
        if feat is None:
            self.skipTest("feat-cmux-next's workflows are not readable here")
        self.assertIn(".github/workflows/cmux-next-base-autofix.yml", self.owned[str(feat)])

class ComparisonSourceTests(unittest.TestCase):
    """Exercise the ordinary guard with distinct fork and official Git owners."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="owned-comparison-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.official = self.directory / "official"
        self.fork = self.directory / "fork"
        self.work = self.directory / "work"
        self.git_env = without_auto_maintenance({
            **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "comparison fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "comparison fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_NO_LAZY_FETCH": "1",
        })
        self.official.mkdir()
        self.checked(self.official, "init", "-q", "-b", "main")
        for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
            self.checked(self.official, "config", key, "true")
        for name in (sources.POOL_SCRIPT, sources.POLICY_SCRIPT):
            self.put(self.official, name, (ROOT / name).read_text())
        self.watched = (".github/workflows/common.yml", ".github/workflows/cmux-next-base-autofix.yml")
        self.put(self.official, sources.RESCUE_SCRIPT,
                 f"WATCHED_WORKFLOW_PATHS = {self.watched!r}\nUNRESCUED_WORKFLOW_PATHS = {{}}\n")
        self.put(self.official, self.watched[0], "on: push\njobs:\n  test:\n    runs-on: glaeda-test\n")
        self.checked(self.official, "add", ".")
        self.checked(self.official, "commit", "-q", "-m", "official main")
        self.main = self.checked(self.official, "rev-parse", "HEAD")
        self.checked(self.official, "checkout", "-q", "-b", FEAT)
        self.put(self.official, self.watched[1],
                 "on: push\njobs:\n  test:\n    runs-on: glaeda-test\n    steps:\n"
                 "      - run: python3 scripts/ci/comparison_picker.py\n")
        self.put(self.official, "scripts/ci/comparison_picker.py", "import pr_runner_pool\n")
        self.put(self.official, "scripts/ci/unrelated_comparison.py", "# not read by the detector\n")
        self.put(self.official, "Sources/NotAComparisonInput.swift", "// must remain blob-less\n")
        self.checked(self.official, "add", ".")
        self.checked(self.official, "commit", "-q", "-m", "official comparison")
        self.feat = self.checked(self.official, "rev-parse", "HEAD")
        self.checked(self.directory, "clone", "-q", "--depth=1", "--single-branch", "--branch=main",
                     self.official.as_uri(), str(self.fork))
        self.put(self.fork, sources.RESCUE_SCRIPT,
                 f"WATCHED_WORKFLOW_PATHS = {(*self.watched, '.github/workflows/fork-only.yml')!r}\n"
                 "UNRESCUED_WORKFLOW_PATHS = {}\n")
        self.checked(self.fork, "add", ".")
        self.checked(self.fork, "commit", "-q", "-m", "fork policy")
        self.checked(self.directory, "clone", "-q", "--depth=1", self.fork.as_uri(), str(self.work))
        self.checked(self.work, "remote", "set-url", "origin", "https://github.com/derrickburns/cmux.git")
        self.environment = {
            "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "derrickburns/cmux",
            "GITHUB_REPOSITORY_ID": "1456369951", "GITHUB_BASE_REF": "main",
            "GITHUB_REF_NAME": "fixture", "CMUX_OWNED_POOL_BRANCH": "main",
            "GH_TOKEN": "fixture-public-read-token",
        }
        self.calls: list[tuple[str, ...]] = []
        self.requests: list[urllib.request.Request] = []
        self.documents: dict[str, object] = {}
        self.fail_fetch = False
        self.origin_is_official = False

    @staticmethod
    def put(root: Path, name: str, text: str) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def command(self, root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                               "-C", str(root), *args], input=stdin, capture_output=True,
                              text=True, env=self.git_env)

    def checked(self, root: Path, *args: str) -> str:
        result = self.command(root, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def transport(self, *args: str, stdin: str | None = None,
                  no_lazy: bool = False) -> subprocess.CompletedProcess:
        self.calls.append(args)
        mapped = list(args)
        if "fetch" in args:
            if self.fail_fetch and "https://github.com/manaflow-ai/cmux.git" in args:
                return subprocess.CompletedProcess(args, 128, "", "controlled official fetch failure")
            mapped = [self.official.as_uri() if arg == "https://github.com/manaflow-ai/cmux.git"
                      else (self.official if self.origin_is_official else self.fork).as_uri()
                      if arg == "origin" else arg for arg in args]
        return self.command(self.work, *mapped, stdin=stdin)

    def listing(self, sha: str, directory: str) -> list[dict]:
        rows = []
        for line in self.checked(self.official, "ls-tree", "-l", sha, directory + "/").splitlines():
            header, path = line.split("\t", 1)
            _, kind, oid, size = header.split()
            rows.append({"name": path.rsplit("/", 1)[-1], "path": path, "sha": oid,
                         "size": int(size) if kind == "blob" else 0,
                         "type": "file" if kind == "blob" else "dir"})
        return rows

    def api(self, request: urllib.request.Request, **kwargs) -> io.BytesIO:
        self.requests.append(request)
        path = request.full_url.removeprefix("https://api.github.com/repos/")
        if path in self.documents:
            body = self.documents[path]
        elif path == "derrickburns/cmux":
            body = {"id": 1456369951, "full_name": "derrickburns/cmux", "fork": True,
                    "owner": {"login": "derrickburns", "type": "User"},
                    "parent": {"id": 1144115288, "full_name": "manaflow-ai/cmux"}}
        elif path.startswith("manaflow-ai/cmux/git/ref/heads/"):
            name = path.rsplit("/", 1)[-1]
            body = {"ref": "refs/heads/" + name,
                    "object": {"type": "commit", "sha": self.main if name == "main" else self.feat}}
        elif path.startswith("manaflow-ai/cmux/contents/"):
            directory, sha = path.removeprefix("manaflow-ai/cmux/contents/").split("?ref=")
            body = self.listing(sha, directory)
        else:
            self.fail(f"unexpected public API input: {path}")
        return io.BytesIO(json.dumps(body).encode())

    def probe(self):
        probe = type("ComparisonProbe", (OwnedPoolSources,), {})
        with patch.dict(globals(), {"ROOT": self.work, "ON_CI": True}), \
                patch.dict(os.environ, self.environment), \
                patch(__name__ + ".git", self.transport), \
                patch("urllib.request.urlopen", self.api):
            probe.setUpClass()
        return probe

    def test_fork_reads_the_official_comparison_without_hydrating_app_or_unread_scripts(self) -> None:
        probe = self.probe()
        self.assertIsNotNone(probe.other, "CI must actually read the official other branch")
        self.assertTrue(probe.ref.startswith("refs/cmux-ci/upstream/manaflow-ai/cmux/"), probe.ref)
        self.assertEqual(self.checked(self.work, "rev-parse", probe.ref), self.feat)
        self.assertIn(self.watched[1], sources.owned_workflows(probe.other))
        try:
            probe("test_run_39638669325s_workflow_is_watched").test_run_39638669325s_workflow_is_watched()
        except unittest.SkipTest as error:
            self.fail(f"the ordinary counterpart assertion must execute for the new namespace: {error}")
        for path in ("Sources/NotAComparisonInput.swift", "scripts/ci/unrelated_comparison.py"):
            oid = self.checked(self.official, "rev-parse", f"{self.feat}:{path}")
            self.assertNotEqual(self.command(self.work, "cat-file", "-e", oid).returncode, 0, path)
        self.assertNotEqual(self.command(self.work, "rev-parse", "--verify", "--quiet",
                                         "refs/remotes/origin/feat-cmux-next").returncode, 0)
        self.assertTrue(self.requests)

    def test_an_existing_origin_comparison_cannot_impersonate_upstream(self) -> None:
        self.checked(self.work, "update-ref", "refs/remotes/origin/feat-cmux-next", "HEAD")
        probe = self.probe()
        self.assertEqual(self.checked(self.work, "rev-parse", probe.ref), self.feat)
        self.assertIn(self.watched[1], sources.owned_workflows(probe.other))

    def test_a_later_ref_change_cannot_replace_the_selected_comparison_bytes(self) -> None:
        self.checked(self.work, "fetch", "-q", "--depth=1", self.official.as_uri(),
                     f"{self.feat}:refs/remotes/origin/feat-cmux-next")
        probe = self.probe()
        self.assertIsNotNone(probe.other)
        self.assertIn(self.watched[1], sources.owned_workflows(probe.other))
        self.checked(self.work, "update-ref", probe.ref, "HEAD")
        self.assertIn(self.watched[1], sources.owned_workflows(probe.other))

    def test_upstream_keeps_the_origin_route_without_an_api_dependency(self) -> None:
        self.origin_is_official = True
        self.environment.update(GITHUB_REPOSITORY="manaflow-ai/cmux", GITHUB_REPOSITORY_ID="1144115288")
        self.checked(self.work, "remote", "set-url", "origin", "https://github.com/manaflow-ai/cmux.git")
        probe = self.probe()
        self.assertEqual(probe.ref, "origin/feat-cmux-next")
        self.assertIn(self.watched[1], sources.owned_workflows(probe.other))
        self.assertEqual(self.requests, [])

    def test_the_current_repository_token_is_not_required_for_public_upstream_metadata(self) -> None:
        self.probe()
        own = [request for request in self.requests
               if request.full_url == "https://api.github.com/repos/derrickburns/cmux"]
        upstream = [request for request in self.requests
                    if request.full_url.startswith("https://api.github.com/repos/manaflow-ai/cmux/")]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0].get_header("Authorization"), "Bearer fixture-public-read-token")
        self.assertTrue(upstream)
        for request in upstream:
            self.assertIsNone(request.get_header("Authorization"), request.full_url)

    def test_wrong_repository_parent_and_ref_are_refused_before_fetch(self) -> None:
        valid = {"id": 1456369951, "full_name": "derrickburns/cmux", "fork": True,
                 "owner": {"login": "derrickburns", "type": "User"},
                 "parent": {"id": 1144115288, "full_name": "manaflow-ai/cmux"}}
        cases = (
            ("derrickburns/cmux", {**valid, "id": 99}),
            ("derrickburns/cmux", {**valid, "full_name": "foreign/cmux"}),
            ("derrickburns/cmux", {**valid, "parent": {"id": 99, "full_name": "foreign/cmux"}}),
            ("manaflow-ai/cmux/git/ref/heads/feat-cmux-next",
             {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.feat}}),
            ("manaflow-ai/cmux/git/ref/heads/feat-cmux-next",
             {"ref": "refs/heads/feat-cmux-next", "object": {"type": "tree", "sha": self.feat}}),
        )
        for path, body in cases:
            with self.subTest(path=path, body=body):
                self.documents = {path: body}
                self.calls.clear()
                with self.assertRaisesRegex(RuntimeError, "comparison"):
                    self.probe()
                self.assertFalse(any("fetch" in call for call in self.calls))

    def test_source_budget_is_checked_before_any_fetch(self) -> None:
        path = f"manaflow-ai/cmux/contents/.github/workflows?ref={self.feat}"
        rows = self.listing(self.feat, ".github/workflows")
        rows[0]["size"] = 17 * 1024 * 1024
        self.documents[path] = rows
        with self.assertRaisesRegex(RuntimeError, "comparison"):
            self.probe()
        self.assertFalse(any("fetch" in call for call in self.calls))

    def test_failed_official_fetch_remains_a_ci_failure(self) -> None:
        self.fail_fetch = True
        with self.assertRaisesRegex(RuntimeError, "comparison"):
            self.probe()

    def test_a_conflicting_cached_namespace_is_not_rebound(self) -> None:
        ref = f"refs/cmux-ci/upstream/manaflow-ai/cmux/{FEAT}/{self.feat}"
        self.checked(self.work, "update-ref", ref, "HEAD")
        with self.assertRaisesRegex(RuntimeError, "comparison"):
            self.probe()
        self.assertFalse(any("fetch" in call for call in self.calls))

    def test_the_main_rescue_copy_stays_authoritative_when_checking_feat(self) -> None:
        self.environment.update(CMUX_OWNED_POOL_BRANCH=FEAT, GITHUB_BASE_REF=FEAT)
        probe = self.probe()
        self.assertEqual(self.checked(self.work, "rev-parse", probe.ref), self.main)
        self.assertIs(probe.rescue_tree, probe.other)
        self.assertEqual(probe.watched, frozenset(self.watched))


if __name__ == "__main__":
    unittest.main()
