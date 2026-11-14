"""gh-merge-green excuses a red check only when the base branch's latest completed run has the same failures."""
import copy
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
            return [{"jobs": [copy.deepcopy(job) for job in self.jobs.values() if job["run_id"] == run]}]
        if "/actions/workflows/7/runs?" in route:
            assert "branch=feat-cmux-next" in route and "status=completed" in route, route
            return {"workflow_runs": copy.deepcopy(self.base_runs)}
        if "/actions/runs/" in route:
            return {"id": int(route.rsplit("/", 1)[1]), "workflow_id": 7}
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
        self.gh.jobs[3] = {"id": 3, "run_id": 10, "head_sha": HEAD, "name": "ci-status", "status": "completed",
                           "conclusion": "failure", "steps": [{"name": "Report", "conclusion": "failure"}]}
        self.with_checks([{"id": 3, "name": "ci-status", "status": "completed", "conclusion": "failure",
                           "app": {"slug": "github-actions"},
                           "details_url": f"https://github.com/{REPO}/actions/runs/10/job/3"}])
        lines = excuse.judge(REPO, "feat-cmux-next", HEAD, [], self.gh, checks=["ci-status"])
        self.assertIn(SWIFT, "\n".join(lines))
        self.assertNotIn("'ci-status'", "\n".join(lines))

    def test_a_red_ci_status_with_no_other_failed_job_blocks(self):
        self.gh.jobs[1]["conclusion"] = "success"
        self.gh.jobs[3] = {"id": 3, "run_id": 10, "head_sha": HEAD, "name": "ci-status", "status": "completed",
                           "conclusion": "failure", "steps": [{"name": "Report", "conclusion": "failure"}]}
        self.with_checks([{"id": 3, "name": "ci-status", "status": "completed", "conclusion": "failure",
                           "app": {"slug": "github-actions"},
                           "details_url": f"https://github.com/{REPO}/actions/runs/10/job/3"}])
        with self.assertRaisesRegex(excuse.Refused, "no failed job"):
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


if __name__ == "__main__":
    unittest.main()
