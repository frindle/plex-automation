#!/usr/bin/env python3
"""Tests for scripts/ci-infra-retry.py, fed saved (synthetic) job JSON."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "ci-infra-retry.py")
FIX = os.path.join(ROOT, "tests", "ci-retry-fixtures")


def run(fixture, attempt=1, *extra):
    p = subprocess.run(
        [sys.executable, "-I", SCRIPT, "--jobs", os.path.join(FIX, fixture + ".json"),
         "--attempt", str(attempt), *extra],
        capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


class Decide(unittest.TestCase):
    def test_no_runner_is_rerun(self):
        self.assertTrue(run("infra-no-runner").startswith("rerun:"))

    def test_lost_runner_failed_with_no_failed_step_is_rerun(self):
        self.assertTrue(run("infra-lost-runner").startswith("rerun:"))

    def test_cancelled_by_github_is_rerun(self):
        self.assertTrue(run("infra-cancelled").startswith("rerun:"))

    def test_cancelled_by_newer_push_is_not_rerun(self):
        self.assertTrue(run("infra-cancelled", 1, "--superseded").startswith("no:"))

    def test_superseded_run_cancelled_before_a_step_is_not_rerun(self):
        # A queued run cancelled by a newer push never ran a step; re-running it
        # would cancel the newer run through the shared concurrency group.
        self.assertTrue(run("infra-no-runner", 1, "--superseded").startswith("no:"))
        self.assertTrue(run("infra-lost-runner", 1, "--superseded").startswith("no:"))

    def test_test_failure_is_never_rerun(self):
        out = run("real-test-failure")
        self.assertTrue(out.startswith("no:") and "Unit tests" in out, out)

    def test_failed_download_in_a_setup_step_is_rerun(self):
        # e.g. chocolatey's feed timed out while installing ffmpeg
        out = run("infra-setup-download")
        self.assertTrue(out.startswith("rerun:") and "Install ffmpeg" in out, out)

    def test_setup_failure_beside_a_failed_check_is_not_rerun(self):
        out = run("setup-and-real-failure")
        self.assertTrue(out.startswith("no:") and "Windows checks" in out, out)

    def test_real_failure_beside_an_infra_one_is_not_rerun(self):
        self.assertTrue(run("mixed-infra-and-real").startswith("no:"))

    def test_attempt_three_is_not_rerun(self):
        self.assertTrue(run("infra-no-runner", 3).startswith("no:"))
        self.assertTrue(run("infra-lost-runner", 3).startswith("no:"))

    def test_attempt_two_still_reruns(self):
        self.assertTrue(run("infra-no-runner", 2).startswith("rerun:"))

    def test_failed_npm_ci_is_rerun_once_only(self):
        # A download blip is retried, but a real lockfile error would fail the same way
        # every time, so an install failure gets one re-run, not the full cap.
        self.assertTrue(run("infra-npm-install", 1).startswith("rerun:"))
        out = run("infra-npm-install", 2)
        self.assertTrue(out.startswith("no:") and "install" in out, out)

    def test_failed_toolchain_setup_is_rerun_up_to_the_cap(self):
        self.assertTrue(run("infra-setup-python", 1).startswith("rerun:"))
        self.assertTrue(run("infra-setup-python", 2).startswith("rerun:"))
        self.assertTrue(run("infra-setup-python", 3).startswith("no:"))

    def test_failed_docker_build_is_never_rerun(self):
        out = run("real-docker-build")
        self.assertTrue(out.startswith("no:") and "build-push-action" in out, out)

    def test_multiline_run_step_is_a_real_failure(self):
        # An unnamed multi-line `run:` is named "Run <script>" and may be a test run.
        self.assertTrue(run("real-multiline-run").startswith("no:"))

    def test_green_run_is_not_rerun(self):
        self.assertTrue(run("all-green").startswith("no:"))

    def test_bad_file_is_a_usage_error(self):
        p = subprocess.run([sys.executable, "-I", SCRIPT, "--jobs", "/nonexistent.json", "--attempt", "1"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
