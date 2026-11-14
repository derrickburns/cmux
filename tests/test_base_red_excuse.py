"""gh-merge-green excuses a red check only when the base branch's latest completed run has the same failures."""
import copy
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("base_red_excuse", ROOT / "scripts/ci/base_red_excuse.py")
excuse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(excuse)

REPO = "manaflow-ai/cmux"
HEAD = "a" * 40
BASE_SHA = "b" * 40
SWIFT = "cmux-next swift test"
SCHEME = "cmux app scheme compile (Debug)"
TEST_STEP = "Run package tests"
ISSUE = ("✘ Test writesTheRequestAfterTheReadyMarkerAndReturnsTheReply() recorded an issue at "
         "DiffSidecarProcessTests.swift:41:9: Expectation failed")
OTHER = "✘ Test palettePagesReleaseTheRegistry() recorded an issue at PaletteNavigationTests.swift:88:3: Expectation failed"


def log(job, step, *lines):
    return "\n".join(f"{job}\t{step}\t2026-10-07T02:10:37.8032610Z {line}" for line in lines)


def failed_job(job_id, run_id, sha, name=SWIFT, step=TEST_STEP):
    return {"id": job_id, "run_id": run_id, "head_sha": sha, "name": name, "status": "completed",
            "conclusion": "failure", "steps": [
                {"name": "Set up runner", "status": "completed", "conclusion": "success"},
                {"name": "Build package and tests", "status": "completed", "conclusion": "success"},
                {"name": step, "status": "completed", "conclusion": "failure"}]}


class FakeGitHub:
    """The head run 10 (workflow 7) and feat-cmux-next's latest completed run 20 of the same workflow."""

    def __init__(self):
        self.jobs = {1: failed_job(1, 10, HEAD), 2: failed_job(2, 20, BASE_SHA)}
        self.logs = {1: log(SWIFT, TEST_STEP, ISSUE, "##[error]1 red tests: writesTheRequest()"),
                     2: log(SWIFT, TEST_STEP, ISSUE, OTHER, "##[error]2 red tests: writesTheRequest(), palette()")}
        self.base_runs = [{"id": 21, "event": "push", "head_branch": "feat-cmux-next", "status": "completed",
                           "conclusion": "cancelled"},
                          {"id": 20, "event": "push", "head_branch": "feat-cmux-next", "status": "completed",
                           "conclusion": "failure", "head_sha": BASE_SHA}]
        self.routes: list[str] = []

    def json(self, route, *, paginate=False):
        self.routes.append(route)
        if "/actions/runs/" in route and route.endswith("/jobs?filter=latest&per_page=100"):
            run = int(route.split("/actions/runs/", 1)[1].split("/", 1)[0])
            jobs = [copy.deepcopy(job) for job in self.jobs.values() if job["run_id"] == run]
            return [{"jobs": jobs, "total_count": len(jobs)}]
        if "/actions/workflows/7/runs?" in route:
            assert "branch=feat-cmux-next" in route and "status=completed" in route, route
            return {"workflow_runs": copy.deepcopy(self.base_runs)}
        if "/actions/runs/" in route:
            run = int(route.rsplit("/", 1)[1])
            return {"id": run, "workflow_id": 7, "head_sha": HEAD if run == 10 else BASE_SHA,
                    "head_branch": "work" if run == 10 else "feat-cmux-next", "run_attempt": 1,
                    "path": ".github/workflows/portable-fixture.yml", "status": "completed", "conclusion": "failure",
                    "event": "pull_request" if run == 10 else "push", "repository": {"full_name": REPO}}
        raise AssertionError(route)

    def log(self, repo, job):
        return self.logs[job["id"]]


class Excusing(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()

    def judge(self, runs=(10,)):
        return excuse.judge(REPO, "feat-cmux-next", HEAD, list(runs), self.gh)

    def test_a_test_failing_on_the_base_too_is_excused_with_the_base_run_named(self):
        lines = self.judge()
        text = "\n".join(lines)
        self.assertIn(SWIFT, text)
        self.assertIn("run 20", text)
        self.assertIn("writesTheRequestAfterTheReadyMarkerAndReturnsTheReply()", text)
        # The cancelled run 21 has no verdict; the newest run with one decides.
        self.assertNotIn("run 21", text)

    def test_counts_do_not_matter_only_names(self):
        # The base fails one more test than the head; the head's are all on the base.
        self.assertTrue(self.judge())

    def test_a_failure_not_on_the_base_blocks(self):
        self.gh.logs[1] = log(SWIFT, TEST_STEP, ISSUE, OTHER.replace("palettePages", "somethingNew"))
        with self.assertRaisesRegex(excuse.Refused, "somethingNew"):
            self.judge()

    def test_a_different_failed_step_blocks(self):
        self.gh.jobs[1]["steps"][1]["conclusion"] = "failure"
        with self.assertRaisesRegex(excuse.Refused, "Build package and tests"):
            self.judge()

    def test_a_compile_error_not_on_the_base_blocks(self):
        self.gh.jobs[1] = failed_job(1, 10, HEAD, name=SCHEME, step="Compile the cmux scheme")
        self.gh.jobs[2] = failed_job(2, 20, BASE_SHA, name=SCHEME, step="Compile the cmux scheme")
        self.gh.logs[1] = log(SCHEME, "Compile the cmux scheme",
                              "Sources/App/New.swift:12:5: error: cannot find 'x' in scope")
        self.gh.logs[2] = log(SCHEME, "Compile the cmux scheme",
                              "Sources/App/Onboarding.swift:30:7: error: type 'ActionSurfaces' has no member 'cli'")
        with self.assertRaisesRegex(excuse.Refused, "cannot find 'x' in scope"):
            self.judge()

    def test_the_same_compile_error_at_a_moved_line_is_excused(self):
        self.gh.jobs[1] = failed_job(1, 10, HEAD, name=SCHEME, step="Compile the cmux scheme")
        self.gh.jobs[2] = failed_job(2, 20, BASE_SHA, name=SCHEME, step="Compile the cmux scheme")
        self.gh.logs[1] = log(SCHEME, "Compile the cmux scheme",
                              "Sources/App/Onboarding.swift:31:7: error: type 'ActionSurfaces' has no member 'cli'")
        self.gh.logs[2] = log(SCHEME, "Compile the cmux scheme",
                              "Sources/App/Onboarding.swift:30:7: error: type 'ActionSurfaces' has no member 'cli'")
        self.assertIn(SCHEME, "\n".join(self.judge()))

    def test_warning_lines_that_differ_between_runs_do_not_block(self):
        # #18147 at 6b8ec44652f against base run 37564446089: the same red test,
        # refused over SwiftPM cache warnings only the head printed.
        self.gh.logs[1] = log(SWIFT, TEST_STEP, ISSUE,
                              "warning: 'swift-collections': skipping cache due to an error: "
                              "The file \u201cmaintenance.lock\u201d doesn\u2019t exist.",
                              "##[warning]'swift-system': skipping cache due to an error: lock")
        self.assertIn(SWIFT, "\n".join(self.judge()))

    def test_failing_tests_decide_and_error_lines_beside_them_are_not_compared(self):
        self.gh.logs[1] = log(SWIFT, TEST_STEP, ISSUE, "##[error]error: the swift test run exited with signal 6")
        self.assertIn(SWIFT, "\n".join(self.judge()))

    def test_a_compile_error_with_a_warning_only_the_head_printed_is_excused(self):
        self.gh.jobs[1] = failed_job(1, 10, HEAD, name=SCHEME, step="Compile the cmux scheme")
        self.gh.jobs[2] = failed_job(2, 20, BASE_SHA, name=SCHEME, step="Compile the cmux scheme")
        error = "Sources/App/Onboarding.swift:30:7: error: type 'ActionSurfaces' has no member 'cli'"
        self.gh.logs[1] = log(SCHEME, "Compile the cmux scheme", error,
                              "Sources/App/Other.swift:9:1: warning: an error: in a warning is noise")
        self.gh.logs[2] = log(SCHEME, "Compile the cmux scheme", error)
        self.assertIn(SCHEME, "\n".join(self.judge()))

    def test_a_job_green_on_the_base_blocks(self):
        self.gh.jobs[2]["conclusion"] = "success"
        with self.assertRaisesRegex(excuse.Refused, "not red on feat-cmux-next"):
            self.judge()

    def test_a_job_the_base_did_not_run_blocks(self):
        del self.gh.jobs[2]
        with self.assertRaisesRegex(excuse.Refused, "no completed feat-cmux-next run"):
            self.judge()

    def test_the_newest_base_run_with_a_verdict_for_the_job_decides(self):
        # 2026-10-07: the newest base push run's swift test was cancelled by the next push.
        self.gh.base_runs.insert(0, {"id": 22, "event": "push", "head_branch": "feat-cmux-next",
                                     "status": "completed", "conclusion": "failure"})
        self.gh.jobs[4] = dict(failed_job(4, 22, BASE_SHA), conclusion="cancelled")
        self.gh.jobs[5] = dict(failed_job(5, 22, BASE_SHA, name=SCHEME))
        self.gh.jobs[5]["steps"] = [{"name": "Set up runner", "status": "completed", "conclusion": "failure"}]
        text = "\n".join(self.judge())
        self.assertIn("run 20", text)
        self.assertNotIn("run 22", text)

    def test_no_completed_base_run_blocks(self):
        self.gh.base_runs = []
        with self.assertRaisesRegex(excuse.Refused, "no completed"):
            self.judge()

    def test_a_setup_refusal_is_never_excused(self):
        self.gh.jobs[1]["steps"] = [{"name": "Set up job", "status": "completed", "conclusion": "success"},
                                    {"name": "Set up runner", "status": "completed", "conclusion": "failure"}]
        self.gh.logs[1] = log(SWIFT, "Set up runner",
                              "##[error]glaeda-cmux-runner-hook: refused: a fleet build is waiting for the host (pid 1)")
        self.gh.jobs[2]["steps"] = copy.deepcopy(self.gh.jobs[1]["steps"])
        self.gh.logs[2] = self.gh.logs[1]
        with self.assertRaisesRegex(excuse.Refused, "refused at setup"):
            self.judge()

    def test_a_job_with_no_steps_is_never_excused(self):
        self.gh.jobs[1]["steps"] = []
        with self.assertRaisesRegex(excuse.Refused, "refused at setup"):
            self.judge()

    def test_a_cancelled_or_timed_out_job_is_never_excused(self):
        for conclusion in ("cancelled", "timed_out"):
            self.gh.jobs[1]["conclusion"] = conclusion
            with self.assertRaisesRegex(excuse.Refused, conclusion):
                self.judge()

    def test_a_failure_with_nothing_to_compare_blocks(self):
        # No test names and no error lines: the same step name alone proves nothing.
        self.gh.logs[1] = log(SWIFT, TEST_STEP, "##[error]Process completed with exit code 1.")
        with self.assertRaisesRegex(excuse.Refused, "nothing to compare"):
            self.judge()

    def test_a_red_run_with_no_failed_job_listed_blocks(self):
        self.gh.jobs[1]["conclusion"] = "success"
        with self.assertRaisesRegex(excuse.Refused, "no failed job"):
            self.judge()

    def test_check_names_resolve_to_their_head_jobs(self):
        self.gh.check_runs = [{"check_runs": [
            {"id": 1, "name": SWIFT, "status": "completed", "conclusion": "failure", "app": {"slug": "github-actions"},
             "details_url": f"https://github.com/{REPO}/actions/runs/10/job/1"}]}]
        original = self.gh.json

        def json_with_checks(route, *, paginate=False):
            if route.endswith(f"commits/{HEAD}/check-runs?per_page=100"):
                return copy.deepcopy(self.gh.check_runs)
            if route.endswith("/actions/jobs/1"):
                return copy.deepcopy(self.gh.jobs[1])
            return original(route, paginate=paginate)

        self.gh.json = json_with_checks
        lines = excuse.judge(REPO, "feat-cmux-next", HEAD, [], self.gh, checks=[SWIFT])
        self.assertIn("run 20", "\n".join(lines))

    def with_checks(self, checks):
        self.gh.check_runs = [{"check_runs": checks}]
        original = self.gh.json

        def json_with_checks(route, *, paginate=False):
            if route.endswith(f"commits/{HEAD}/check-runs?per_page=100"):
                return copy.deepcopy(self.gh.check_runs)
            if "/actions/jobs/" in route:
                return copy.deepcopy(self.gh.jobs[int(route.rsplit("/", 1)[1])])
            return original(route, paginate=paginate)

        self.gh.json = json_with_checks

    def test_a_red_ci_status_is_judged_by_the_failed_jobs_of_its_run(self):
        # The required check needs its real declared routing context, not a
        # generic name-only "Report" failure that could hide a routing fault.
        self.gh = DeclaredDependencyGitHub()
        lines = excuse.judge(REPO, "feat-cmux-next", HEAD, [], self.gh, checks=["ci-status"])
        self.assertIn("Fast static checks", "\n".join(lines))
        self.assertIn("unexecuted", "\n".join(lines))

    def test_a_red_ci_status_with_no_other_failed_job_blocks(self):
        self.gh = DeclaredDependencyGitHub()
        for logical in ("static-preflight", "linux-preflight"):
            self.gh.logical[10][logical]["conclusion"] = "success"
        with self.assertRaises(excuse.Refused):
            excuse.judge(REPO, "feat-cmux-next", HEAD, [], self.gh, checks=["ci-status"])


def crashed(job, signal, unfinished, frames, *extra):
    """A swift test log whose test helper died on `signal` with `unfinished` tests in flight."""
    return log(job, TEST_STEP, *extra,
               f"error: Process '/x/swiftpm-testing-helper --test-bundle-path /x/CmuxNextPackageTests' "
               f"exited with unexpected signal code {signal}",
               f"##[error]no \"Test run with\" summary line (crash, kill or truncation); {len(unfinished)} tests "
               f"started and never finished: {'; '.join(unfinished)}",
               f"##[error]the test helper died on signal {signal}; crashed thread: {frames}")


class BaseOwnedCrash(unittest.TestCase):
    """A test helper crash on the base is the base's: the head's crash with the same signal is excused,
    though the tests in flight and the crashed thread differ run to run."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.logs[2] = crashed(SWIFT, 5, ["a()", "b()"], "swift_task_checkIsolatedSwift <- KeyWindowObserver.windowWillClose(_:)")

    def judge(self):
        return excuse.judge(REPO, "feat-cmux-next", HEAD, [10], self.gh)

    def test_a_crash_with_the_signal_the_base_crashed_on_is_excused(self):
        self.gh.logs[1] = crashed(SWIFT, 5, ["c()", "d()", "e()"], "swift_task_checkIsolatedSwift <- ???")
        text = "\n".join(self.judge())
        self.assertIn("run 20", text)
        self.assertIn("signal 5", text)

    def test_a_crash_the_base_did_not_have_blocks(self):
        self.gh.logs[1] = crashed(SWIFT, 5, ["c()"], "f")
        self.gh.logs[2] = log(SWIFT, TEST_STEP, ISSUE)
        with self.assertRaisesRegex(excuse.Refused, "signal"):
            self.judge()

    def test_a_crash_on_another_signal_blocks(self):
        self.gh.logs[1] = crashed(SWIFT, 11, ["c()"], "f")
        with self.assertRaisesRegex(excuse.Refused, "signal"):
            self.judge()

    def test_a_red_test_before_the_crash_must_still_be_on_the_base(self):
        self.gh.logs[1] = crashed(SWIFT, 5, ["c()"], "f", OTHER)
        with self.assertRaisesRegex(excuse.Refused, "palettePagesReleaseTheRegistry"):
            self.judge()
        self.gh.logs[2] = crashed(SWIFT, 5, ["a()"], "g", OTHER)
        self.assertTrue(self.judge())

class Script(unittest.TestCase):
    def test_gh_merge_green_asks_the_base_before_refusing_a_red_lane(self):
        text = (ROOT / "scripts/gh-merge-green").read_text()
        self.assertIn("base_red_excuse.py", text)
        # Lint stays unwaivable, and --override still skips the base comparison.
        self.assertIn("--override cannot bypass god-file, l10n or concurrency lint", text)


class StructuredStaticPreflight(unittest.TestCase):
    """Sanitized complete bodies from base 43607456566 and head 43606270780.

    The expired flags are public source diagnostics, not generic FAILED markers.
    Keep their paths, flag identities and review dates literal when comparing.
    """

    JOB = "Fast static checks"
    STEP = "Run the contributor static preflight"
    PASSED = ("xcstrings", "localization", "localization-defaults", "project-tests", "project",
              "config-schema", "test-wiring-sync", "wire-app-sources", "ui-lab", "ui-fuzzer",
              "launch-policy", "test-wiring", "package-groups", "remote-tmux-waits-tests", "remote-tmux-waits")
    CAUSES = (
        "Packages/Shared/CmuxClientConfig/Sources/CmuxClientConfig/ClientConfigFlag.swift: "
        "'ios-feed-performance-release' reviewBy 2026-11-06 has passed — "
        "remove the zombie flag or consciously extend the date",
        "Sources/FeatureFlags.swift: 'agent-inbox-quick-view-enabled-release' reviewBy 2026-10-15 "
        "has passed — remove the zombie flag or consciously extend the date",
        "Sources/FeatureFlags.swift: 'conversation-sidebar-release' reviewBy 2026-10-18 "
        "has passed — remove the zombie flag or consciously extend the date",
    )

    def body(self, causes=None, *, duration="2.08", raw=False):
        lines = [f"PASSED {name} (0.12s)" for name in self.PASSED]
        lines += [f"FAILED feature-flags ({duration}s)", "lint-feature-flags: FAILED"]
        lines += [f"  - {cause}" for cause in (self.CAUSES if causes is None else causes)]
        lines += ["Rerun from this checkout: python3 scripts/verify-local.py --only feature-flags",
                  "FAILED: 16/16 selected checks ran. Native compilation, app tests and app launch were not checked.",
                  "Next for Swift changes: use the normal native build/test workflow in CONTRIBUTING.md.",
                  "##[error]Process completed with exit code 1."]
        if raw:
            return "\n".join(f"2026-11-14T14:43:05.1741148Z {line}" for line in lines)
        return log(self.JOB, self.STEP, *lines)

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.jobs = {1: failed_job(1, 10, HEAD, self.JOB, self.STEP),
                        2: failed_job(2, 20, BASE_SHA, self.JOB, self.STEP)}
        self.gh.base_runs[1]["workflow_id"] = 7
        self.gh.logs = {1: self.body(raw=True), 2: self.body(duration="3.64")}

    def judge(self):
        return excuse.judge(REPO, "feat-cmux-next", HEAD, [10], self.gh)

    def test_complete_retained_failure_bodies_match_across_both_log_formats(self):
        audit = "\n".join(self.judge())
        self.assertIn("run 20", audit)
        self.assertIn("ios-feed-performance-release", audit)
        self.assertIn("2026-11-06", audit)

    def test_the_base_may_have_an_additional_complete_cause(self):
        self.gh.logs[2] = self.body(self.CAUSES + (self.CAUSES[0].replace("ios-feed", "ios-other"),))
        self.assertTrue(self.judge())

    def test_runtime_durations_timestamps_and_passing_test_counts_are_not_causes(self):
        self.gh.logs[1] = self.body(duration="9.99", raw=True).replace("PASSED project-tests (0.12s)",
                                                                    "PASSED project-tests (1.10s; 15 tests executed, 1 skipped)")
        self.assertTrue(self.judge())

    def test_changed_flag_date_path_or_diagnostic_text_is_not_excused(self):
        changes = (("ios-feed", "ios-other"), ("2026-11-06", "2026-11-07"),
                   ("ClientConfigFlag.swift", "OtherFlag.swift"), ("has passed", "has changed"))
        for before, after in changes:
            with self.subTest(change=after):
                self.gh.logs[1] = self.body(raw=True).replace(before, after)
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_a_new_head_cause_is_not_excused(self):
        self.gh.logs[1] = self.body(self.CAUSES + (self.CAUSES[0].replace("ios-feed", "ios-new"),), raw=True)
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_missing_empty_or_unknown_cause_bodies_are_never_authority(self):
        bodies = ((), ("unknown feature flag failure",), ("FAILED",))
        for causes in bodies:
            with self.subTest(causes=causes):
                self.gh.logs = {1: self.body(causes, raw=True), 2: self.body(causes)}
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_truncation_or_missing_verifier_boundaries_refuses_even_on_both_runs(self):
        for omitted in ("lint-feature-flags: FAILED", "Rerun from this checkout:", "FAILED: 16/16",
                        "Next for Swift changes:", "##[error]Process completed"):
            with self.subTest(omitted=omitted):
                self.gh.logs = {1: "\n".join(line for line in self.body(raw=True).splitlines() if omitted not in line),
                                2: "\n".join(line for line in self.body().splitlines() if omitted not in line)}
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_a_partial_or_inconsistent_execution_summary_refuses(self):
        for summary in ("15/16", "17/17", "0/0"):
            with self.subTest(summary=summary):
                self.gh.logs = {1: self.body(raw=True).replace("16/16", summary),
                                2: self.body().replace("16/16", summary)}
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_extra_unknown_failed_check_or_body_is_not_hidden_by_known_causes(self):
        for extra in ("FAILED unknown-check (0.10s)", "unknown additional cause",
                      "##[error]unknown additional cause"):
            with self.subTest(extra=extra):
                self.gh.logs[1] = self.body(raw=True).replace("Rerun from this checkout:", extra + "\nRerun from this checkout:")
                self.gh.logs[2] = self.body().replace("Rerun from this checkout:", extra + "\nRerun from this checkout:")
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_duplicate_check_or_diagnostic_records_refuse(self):
        for before, after in (("PASSED xcstrings (0.12s)", "PASSED localization (0.12s)"),
                              (self.CAUSES[1], self.CAUSES[0])):
            with self.subTest(before=before):
                self.gh.logs = {1: self.body(raw=True).replace(before, after),
                                2: self.body().replace(before, after)}
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_missing_base_body_does_not_become_generic_failed_step_authority(self):
        self.gh.logs[2] = log(self.JOB, self.STEP, "FAILED feature-flags (3.64s)",
                              "##[error]Process completed with exit code 1.")
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_new_error_or_test_beside_the_structured_body_still_refuses(self):
        for extra in ("##[error]error: new compiler failure", OTHER):
            with self.subTest(extra=extra):
                self.gh.logs[1] = self.body(raw=True) + "\n" + extra
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_failing_test_cannot_hide_a_new_structured_cause(self):
        self.gh.logs[1] = self.body(raw=True).replace("2026-11-06", "2026-11-07") + "\n" + ISSUE
        self.gh.logs[2] = self.body() + "\n" + ISSUE
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_matching_test_cannot_hide_an_additional_preflight_error(self):
        self.gh.logs[1] = self.body(raw=True) + "\n" + ISSUE + "\n##[error]unknown additional cause"
        self.gh.logs[2] = self.body() + "\n" + ISSUE
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_unknown_errors_outside_the_complete_body_are_not_attribution(self):
        self.gh.logs[1] = self.body(raw=True) + "\n##[error]unknown additional cause"
        self.gh.logs[2] = self.body() + "\n##[error]unknown additional cause"
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_head_source_mismatch_is_not_excused(self):
        self.gh.jobs[1]["head_sha"] = "c" * 40
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_base_source_mismatch_is_not_excused(self):
        self.gh.jobs[2]["head_sha"] = "c" * 40
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_base_workflow_mismatch_is_not_excused(self):
        self.gh.base_runs[1]["workflow_id"] = 8
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_base_branch_mismatch_is_not_excused(self):
        self.gh.base_runs[1]["head_branch"] = "another-base"
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_job_or_failed_step_mismatch_is_not_excused(self):
        for field in ("job", "step"):
            with self.subTest(field=field):
                self.setUp()
                if field == "job":
                    self.gh.jobs[2]["name"] = "Another static job"
                else:
                    self.gh.jobs[1]["steps"][-1]["name"] = "Another failed step"
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_tabbed_log_job_or_step_mismatch_is_not_excused(self):
        for field in (self.JOB, self.STEP):
            with self.subTest(field=field):
                self.gh.logs[1] = self.body().replace(field, "another-owner")
                with self.assertRaises(excuse.Refused):
                    self.judge()


CI_NEEDS = ("changes", "fast-guards", "claude-wrapper", "suite-coverage", "ui-tests",
            "static-preflight", "guards", "ghosttykit-release-check", "browser", "remote-daemon",
            "web", "linux-preflight", "macos-admission-gate", "macos")
LINUX_NEEDS = ("changes", "static-preflight", "fast-guards", "guards", "ghosttykit-release-check", "web")
CI_SOURCE = (ROOT / ".github/workflows/ci.yml").read_bytes()
CI_SOURCE_HASH = "e13d007598ac5617fe87a327ef5273bf97df7b189bf937e7ab8d2631c204cb8d"


def retained_program(first, last):
    """Exact reviewed workflow echo, not an execution of its Python program."""
    lines = CI_SOURCE.decode().splitlines()
    return "\n".join(line[10:] for line in lines[first - 1:last]).rstrip()


LINUX_PROGRAM = retained_program(1447, 1534)
PLATFORM_PROGRAM = retained_program(1749, 1828)


def dependency_log(name, needs, body):
    step, variable, program = (("Check routed Linux results", "PREFLIGHT_NEEDS", LINUX_PROGRAM)
                               if name == "linux-preflight" else
                               ("Check platform workflow routing", "PLATFORM_NEEDS", PLATFORM_PROGRAM))
    encoded = json.dumps(needs, indent=2).splitlines()
    return log(name, step, "##[group]Run python3 - <<'PY'",
               *["^[[36;1m" + line + "^[[0m" for line in program.rstrip("\n").split("\n")],
               "shell: /usr/bin/bash -e {0}", "env:",
               "  CI_CACHE_R2_PUBLIC_URL: https://ci-cache.cmux.com",
               '  CI_OWNED_HEAD_REPOS: ["manaflow-ai/cmux", "teamleaderleo/cmux"]',
               "  " + variable + ": " + encoded[0], *encoded[1:],
               "##[endgroup]", *body, "##[error]Process completed with exit code 1.")


class DeclaredDependencyGitHub:
    """Sanitized initial-attempt shape of the retained base/e3 CI graph.

    IDs and ordinary successful step names are synthetic. The complete workflow
    and the two echoed programs come from the real pinned source. No gh/process
    or workflow program executes through this boundary.
    """

    def __init__(self):
        assert hashlib.sha256(CI_SOURCE).hexdigest() == CI_SOURCE_HASH
        assert hashlib.sha256(LINUX_PROGRAM.encode()).hexdigest() == "49f81e56acc38ce06ae7468c11abedbd6aa6e4fa5ab26176edcdab60dae137d0"
        assert hashlib.sha256(PLATFORM_PROGRAM.encode()).hexdigest() == "4e52775680a43a6eacfe0584229e78162fb58b5eb47c2cac0ab42ce91437784c"
        self.routes = []
        self.jobs, self.logs, self.logical, self.needs = {}, {}, {}, {}
        self.runs = {run: {"id": run, "workflow_id": 7, "head_sha": sha,
                           "head_branch": branch, "path": ".github/workflows/ci.yml",
                           "status": "completed", "conclusion": "failure", "run_attempt": 1,
                           "event": event, "repository": {"full_name": REPO}}
                     for run, sha, branch, event in [(10, HEAD, "work", "pull_request"),
                                                      (20, BASE_SHA, "feat-cmux-next", "workflow_dispatch")]}
        self.workflow_source = CI_SOURCE
        for run, sha in ((10, HEAD), (20, BASE_SHA)):
            outputs = dict.fromkeys(("macos", "cli", "unit_suite", "linux_guard_tests", "linux_guard_history",
                                     "linux_guard_cli", "linux_guard_source", "ghosttykit_release"), "true")
            outputs.update(dict.fromkeys(("full_suite", "swift_packages", "web", "agent_session_web",
                                          "browser", "remote_daemon", "claude_wrapper", "coverage_gap"), "false"))
            outputs["source_parent1"] = BASE_SHA if run == 10 else "c" * 40
            outputs["coverage_fingerprint"] = (f"v1;sha={sha};ref={self.runs[run]['head_branch']};"
                                                 "cache=r2;release=universal;coverage=" +
                                                 ("compile-only" if run == 10 else "full"))
            if run == 20:
                outputs.update(dict.fromkeys(("full_suite", "swift_packages", "web", "agent_session_web",
                                              "browser", "remote_daemon", "claude_wrapper"), "true"))
            states = dict.fromkeys(CI_NEEDS, "skipped")
            states.update({"changes": "success", "fast-guards": "success", "guards": "success",
                           "ghosttykit-release-check": "success", "static-preflight": "failure",
                           "linux-preflight": "failure", "ci-status": "failure"})
            if run == 20:
                states.update({"fast-guards": "failure", "web": "failure", "browser": "success"})
            self.needs[run] = {name: {"result": states[name], "outputs": outputs if name == "changes" else {}}
                               for name in CI_NEEDS}
            self.logical[run] = {}
            for index, name in enumerate((*CI_NEEDS, "ci-status"), 1):
                display = {"fast-guards": "CI fast guards", "static-preflight": "Fast static checks",
                           "claude-wrapper": "Claude wrapper regressions", "ghosttykit-release-check": "GhosttyKit release check",
                           "macos-admission-gate": "macOS admission gate"}.get(name, name)
                if name in ("guards", "web", "browser") and states[name] != "skipped":
                    display = name + " / " + ("Web status" if name == "web" else "checks")
                step = "Run the contributor static preflight" if name == "static-preflight" else "Check routed Linux results" if name == "linux-preflight" else "Check platform workflow routing" if name == "ci-status" else "Run"
                steps = [] if states[name] == "skipped" else [
                    {"name": "Set up job", "status": "completed", "conclusion": "success"},
                    {"name": step, "status": "completed", "conclusion": states[name]},
                    *([{"name": "Check routed CI jobs", "status": "completed", "conclusion": "skipped"}]
                      if name == "ci-status" else []),
                    {"name": "Complete job", "status": "completed", "conclusion": "success"}]
                item = {"id": index + (100 if run == 20 else 0), "run_id": run, "head_sha": sha,
                        "name": display, "status": "completed", "conclusion": states[name], "steps": steps}
                self.jobs[item["id"]] = item
                self.logical[run][name] = item
            self.refresh_logs(run)
        self.check_runs = [{"check_runs": [{"id": 999, "name": "ci-status", "status": "completed",
                           "conclusion": "failure", "app": {"slug": "github-actions"},
                           "details_url": f"https://github.com/{REPO}/actions/runs/10/job/{self.logical[10]['ci-status']['id']}"}]}]
        self.page_delta = 0

    def refresh_logs(self, run):
        n = self.needs[run]
        linux_body = ["static-preflight: failure"]
        if run == 20:
            linux_body += ["fast-guards: failure", "web: failure (one or more web routes=true)"]
        self.logs[self.logical[run]["linux-preflight"]["id"]] = dependency_log(
            "linux-preflight", {k: n[k] for k in LINUX_NEEDS}, linux_body)
        self.logs[self.logical[run]["ci-status"]["id"]] = dependency_log(
            "ci-status", n, ["linux preflight did not pass: failure"])
        self.logs[self.logical[run]["static-preflight"]["id"]] = StructuredStaticPreflight().body()

    def json(self, route, *, paginate=False):
        self.routes.append(route)
        if "/contents/.github/workflows/ci.yml?ref=" in route:
            raw = self.workflow_source
            return {"type": "file", "path": ".github/workflows/ci.yml", "encoding": "base64",
                    "size": len(raw), "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
                    "content": base64.b64encode(raw).decode()}
        if route.endswith(f"commits/{HEAD}/check-runs?per_page=100"):
            return copy.deepcopy(self.check_runs)
        if "/actions/workflows/" in route and "/runs?" in route:
            return {"workflow_runs": [copy.deepcopy(self.runs[20])] if "/workflows/7/" in route else []}
        if "/actions/jobs/" in route:
            return copy.deepcopy(self.jobs[int(route.rsplit("/", 1)[1])])
        if "/actions/runs/" in route:
            tail = route.split("/actions/runs/", 1)[1]
            run = int(tail.split("/", 1)[0])
            if "/jobs?" in tail:
                jobs = [copy.deepcopy(x) for x in self.jobs.values() if x["run_id"] == run]
                result = {"total_count": len(jobs) + self.page_delta, "jobs": jobs}
                return [result] if paginate else result
            return copy.deepcopy(self.runs[run])
        raise AssertionError(route)

    def log(self, repo, job):
        return self.logs[job["id"]]


class DependencyRollups(unittest.TestCase):
    def setUp(self):
        self.gh = DeclaredDependencyGitHub()

    def judge(self, checks=False):
        return excuse.judge(REPO, "feat-cmux-next", HEAD, [] if checks else [10], self.gh,
                            checks=["ci-status"] if checks else [])

    def test_complete_initial_dependency_chain_closes_through_both_callers(self):
        for checks in (False, True):
            with self.subTest(checks=checks):
                self.setUp()
                audit = "\n".join(self.judge(checks))
                for marker in ("Fast static checks", "linux-preflight", "ci-status", "unexecuted"):
                    self.assertIn(marker, audit)

    def test_name_only_ci_status_failure_is_not_dependency_proof(self):
        for checks in (False, True):
            with self.subTest(checks=checks):
                legacy = Excusing()
                legacy.setUp()
                gh = legacy.gh
                gh.jobs[3] = failed_job(3, 10, HEAD, "ci-status", "Independent route decision")
                gh.logs[3] = log("ci-status", "Independent route decision", "invalid independent route")
                legacy.with_checks([{"id": 3, "name": "ci-status", "status": "completed", "conclusion": "failure",
                                     "app": {"slug": "github-actions"},
                                     "details_url": f"https://github.com/{REPO}/actions/runs/10/job/3"}])
                with self.assertRaises(excuse.Refused):
                    excuse.judge(REPO, "feat-cmux-next", HEAD, [] if checks else [10], gh,
                                 checks=["ci-status"] if checks else [])

    def test_unknown_ci_status_body_cannot_hide_behind_the_failed_leaf(self):
        j = self.gh.logical[10]["ci-status"]
        self.gh.logs[j["id"]] = log("ci-status", "Check platform workflow routing", "independent routing failure")
        with self.assertRaises(excuse.Refused):
            self.judge(True)

    def test_source_workflow_run_and_attempt_mismatches_refuse(self):
        changes = ((10, "head_sha", "d" * 40), (20, "head_sha", "d" * 40),
                   (10, "workflow_id", 9), (20, "head_branch", "other-base"),
                   (10, "path", ".github/workflows/other.yml"), (10, "run_attempt", 2))
        for run, key, value in changes:
            with self.subTest(run=run, key=key):
                self.setUp()
                self.gh.runs[run][key] = value
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_changed_workflow_bytes_refuse_without_executing_them(self):
        self.gh.workflow_source += b"\n# different graph\n"
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_partial_program_environment_and_body_boundaries_refuse(self):
        before_after = (("import json", "import math"), ("PREFLIGHT_NEEDS", "OTHER_NEEDS"),
                        ("##[endgroup]", ""), ("static-preflight: failure", ""),
                        ("##[error]Process completed with exit code 1.", ""))
        for before, after in before_after:
            with self.subTest(before=before):
                self.setUp()
                j = self.gh.logical[10]["linux-preflight"]
                self.gh.logs[j["id"]] = self.gh.logs[j["id"]].replace(before, after)
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_missing_extra_or_changed_needs_nodes_refuse(self):
        for action in ("missing", "extra", "changed"):
            with self.subTest(action=action):
                self.setUp()
                if action == "missing":
                    del self.gh.needs[10]["browser"]
                elif action == "extra":
                    self.gh.needs[10]["unknown"] = {"result": "success", "outputs": {}}
                else:
                    self.gh.needs[10]["browser"]["result"] = "success"
                self.gh.refresh_logs(10)
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_invalid_or_missing_guard_web_ghostty_and_later_routes_refuse(self):
        for route in ("linux_guard_source", "web", "ghosttykit_release", "macos", "claude_wrapper", "full_suite"):
            for value in (None, "unknown"):
                with self.subTest(route=route, value=value):
                    self.setUp()
                    out = self.gh.needs[10]["changes"]["outputs"]
                    if value is None:
                        del out[route]
                    else:
                        out[route] = value
                    self.gh.refresh_logs(10)
                    with self.assertRaises(excuse.Refused):
                        self.judge()

    def test_required_work_skip_and_downstream_independent_faults_refuse(self):
        for name in ("guards", "ghosttykit-release-check", "macos", "claude-wrapper", "ui-tests"):
            with self.subTest(name=name):
                self.setUp()
                state = "skipped" if name in ("guards", "ghosttykit-release-check") else "failure"
                self.gh.logical[10][name]["conclusion"] = state
                if state == "skipped":
                    self.gh.logical[10][name]["steps"] = []
                self.gh.needs[10][name]["result"] = state
                self.gh.refresh_logs(10)
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_extra_unknown_diagnostic_even_on_both_runs_refuses(self):
        for run in (10, 20):
            j = self.gh.logical[run]["linux-preflight"]
            self.gh.logs[j["id"]] += "\n" + log("linux-preflight", "Check routed Linux results", "unknown fault")
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_wrong_job_display_or_step_mapping_refuses(self):
        for name, field in (("static-preflight", "name"), ("linux-preflight", "steps")):
            with self.subTest(name=name):
                self.setUp()
                j = self.gh.logical[10][name]
                if field == "name":
                    j[field] = "Another static checker"
                else:
                    j[field][1]["name"] = "Another step"
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_duplicate_logical_display_or_job_id_and_partial_pages_refuse(self):
        for kind in ("display", "id", "page"):
            with self.subTest(kind=kind):
                self.setUp()
                j = copy.deepcopy(self.gh.logical[10]["guards"])
                if kind == "page":
                    self.gh.page_delta = 1
                else:
                    if kind == "display":
                        j["id"] = 999
                    self.gh.jobs[999] = j
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_other_failed_head_job_is_still_audited(self):
        j = failed_job(500, 10, HEAD, "Independent check", "Independent step")
        self.gh.jobs[500] = j
        self.gh.logs[500] = log(j["name"], "Independent step", "error: head-only failure")
        with self.assertRaises(excuse.Refused):
            self.judge(True)

    def test_new_leaf_cause_is_not_excused_by_the_dependency_summary(self):
        j = self.gh.logical[10]["static-preflight"]
        self.gh.logs[j["id"]] = self.gh.logs[j["id"]].replace("2026-11-06", "2026-11-07")
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_cancellation_timeout_and_setup_refusal_are_never_dependency_authority(self):
        for state in ("cancelled", "timed_out", "setup"):
            with self.subTest(state=state):
                self.setUp()
                j = self.gh.logical[10]["static-preflight"]
                if state == "setup":
                    j["steps"][0]["conclusion"] = "failure"
                else:
                    j["conclusion"] = state
                with self.assertRaises(excuse.Refused):
                    self.judge()

    def test_later_ci_step_must_remain_explicitly_unexecuted(self):
        j = self.gh.logical[10]["ci-status"]
        j["steps"][2]["conclusion"] = "failure"
        with self.assertRaises(excuse.Refused):
            self.judge(True)

    def test_job_run_source_and_completed_verdict_are_exact(self):
        for field, value in (("run_id", 99), ("head_sha", "c" * 40),
                             ("status", "in_progress"), ("id", 777)):
            with self.subTest(field=field):
                self.setUp()
                # For ID, challenge the exact /jobs/<id> response selected by
                # the required-check URL, not an unrelated nonexistent log.
                target = "ci-status" if field == "id" else "linux-preflight"
                self.gh.logical[10][target][field] = value
                with self.assertRaises(excuse.Refused):
                    self.judge(True)

    def test_duplicate_needs_json_keys_are_not_last_value_authority(self):
        j = self.gh.logical[10]["linux-preflight"]
        self.gh.logs[j["id"]] = self.gh.logs[j["id"]].replace(
            '"result": "success",', '"result": "failure", "result": "success",', 1)
        with self.assertRaises(excuse.Refused):
            self.judge()

    def test_unexpected_successful_rollup_step_is_not_hidden(self):
        j = self.gh.logical[10]["ci-status"]
        j["steps"].insert(1, {"name": "Independent route decision", "status": "completed", "conclusion": "success"})
        with self.assertRaises(excuse.Refused):
            self.judge(True)

    def test_run_attempt_change_during_leaf_observation_refuses(self):
        original = self.gh.log

        def changed(repo, job):
            value = original(repo, job)
            if job["name"] == "Fast static checks":
                self.gh.runs[10]["run_attempt"] = 2
            return value

        self.gh.log = changed
        with self.assertRaises(excuse.Refused):
            self.judge()


class CompleteRunInventories(unittest.TestCase):
    """Every selected run is complete before choosing any failed job to audit."""

    def fixture(self):
        gh = DeclaredDependencyGitHub()
        gh.jobs[500] = failed_job(500, 10, HEAD, "CI timing", "Independent step")
        gh.logs[500] = log("CI timing", "Independent step", "error: independent head-only cause")
        return gh

    def judge(self, gh, checks=False):
        return excuse.judge(REPO, "feat-cmux-next", HEAD, [] if checks else [10], gh,
                            checks=["ci-status"] if checks else [])

    def first_inventory(self, gh, change):
        original = gh.json
        count = 0

        def selected(route, *, paginate=False):
            nonlocal count
            value = original(route, paginate=paginate)
            if route == f"repos/{REPO}/actions/runs/10/jobs?filter=latest&per_page=100":
                count += 1
                if count == 1:
                    return change(value)
            return value

        gh.json = selected

    def test_complete_inventory_does_not_excuse_an_unrelated_failed_job(self):
        for checks in (False, True):
            with self.subTest(checks=checks), self.assertRaises(excuse.Refused):
                self.judge(self.fixture(), checks)

    def test_partial_first_inventory_cannot_hide_an_unrelated_failed_job(self):
        for checks in (False, True):
            with self.subTest(checks=checks):
                gh = self.fixture()

                def omit(pages):
                    pages[0]["jobs"] = [j for j in pages[0]["jobs"] if j["id"] != 500]
                    return pages

                self.first_inventory(gh, omit)
                with self.assertRaises(excuse.Refused):
                    self.judge(gh, checks)

    def test_changed_complete_snapshot_is_refused_instead_of_adopting_added_jobs(self):
        for checks in (False, True):
            with self.subTest(checks=checks):
                gh = self.fixture()

                def omit_with_count(pages):
                    pages[0]["jobs"] = [j for j in pages[0]["jobs"] if j["id"] != 500]
                    pages[0]["total_count"] -= 1
                    return pages

                self.first_inventory(gh, omit_with_count)
                with self.assertRaises(excuse.Refused):
                    self.judge(gh, checks)

    def test_missing_initial_count_is_not_complete_inventory(self):
        gh = DeclaredDependencyGitHub()

        def missing(pages):
            del pages[0]["total_count"]
            return pages

        self.first_inventory(gh, missing)
        with self.assertRaises(excuse.Refused):
            self.judge(gh)

    def test_initial_duplicate_id_or_display_is_not_overwritten(self):
        for key in ("id", "name"):
            with self.subTest(key=key):
                gh = DeclaredDependencyGitHub()

                def duplicate(pages):
                    rows = pages[0]["jobs"]
                    rows[1][key] = rows[0][key]
                    return pages

                self.first_inventory(gh, duplicate)
                with self.assertRaises(excuse.Refused):
                    self.judge(gh)

    def test_initial_foreign_job_source_run_or_status_refuses(self):
        for field, value in (("run_id", 99), ("head_sha", "d" * 40), ("status", "in_progress")):
            with self.subTest(field=field):
                gh = DeclaredDependencyGitHub()

                def changed(pages):
                    pages[0]["jobs"][0][field] = value
                    return pages

                self.first_inventory(gh, changed)
                with self.assertRaises(excuse.Refused):
                    self.judge(gh)

    def test_changed_successful_job_snapshot_is_not_ignored(self):
        gh = DeclaredDependencyGitHub()
        gh.jobs[500] = dict(failed_job(500, 10, HEAD, "CI timing", "Metrics"), conclusion="success")
        for step in gh.jobs[500]["steps"]:
            step["conclusion"] = "success"

        def changed(pages):
            next(j for j in pages[0]["jobs"] if j["id"] == 500)["steps"][-1]["name"] = "Different metrics"
            return pages

        self.first_inventory(gh, changed)
        with self.assertRaises(excuse.Refused):
            self.judge(gh)

    def test_reordered_complete_pages_keep_the_same_inventory(self):
        gh = DeclaredDependencyGitHub()

        def reordered(pages):
            rows = list(reversed(pages[0]["jobs"]))
            count = pages[0]["total_count"]
            return [{"jobs": rows[:5], "total_count": count}, {"jobs": rows[5:], "total_count": count}]

        self.first_inventory(gh, reordered)
        self.assertEqual(len(self.judge(gh)), 3)

    def test_non_rollup_required_check_still_audits_all_failed_jobs_of_its_run(self):
        owner = Excusing()
        owner.setUp()
        gh = owner.gh
        gh.jobs[500] = failed_job(500, 10, HEAD, "CI timing", "Independent step")
        gh.logs[500] = log("CI timing", "Independent step", "error: independent head-only cause")
        owner.with_checks([{"id": 1, "name": SWIFT, "status": "completed", "conclusion": "failure",
                            "app": {"slug": "github-actions"},
                            "details_url": f"https://github.com/{REPO}/actions/runs/10/job/1"}])
        with self.assertRaises(excuse.Refused):
            excuse.judge(REPO, "feat-cmux-next", HEAD, [], gh, checks=[SWIFT])

    def test_attempt_change_inside_inventory_collection_refuses(self):
        gh = DeclaredDependencyGitHub()

        def changed(pages):
            gh.runs[10]["run_attempt"] = 2
            return pages

        self.first_inventory(gh, changed)
        with self.assertRaises(excuse.Refused):
            self.judge(gh)


if __name__ == "__main__":
    unittest.main()
