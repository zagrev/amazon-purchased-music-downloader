import subprocess
import unittest
from unittest.mock import patch

from release_version import release_plan, remote_tags, reserve_release


class ReleaseVersionTests(unittest.TestCase):
    def test_first_release_and_numeric_patch_increment(self):
        self.assertEqual(release_plan({}, [], "commit")["version"], "0.1.0")
        self.assertEqual(release_plan({
            "v0.1.9": "a", "v0.1.10": "b", "v0.1.2": "c", "v0.2.0-beta": "d",
        }, [], "commit")["version"], "0.1.11")
        self.assertEqual(release_plan({"v1.0.0": "a", "v0.9.99": "b"}, [], "commit")["version"], "1.0.1")

    def test_new_commit_increments_past_reserved_tags_and_drafts(self):
        releases = [{"tag_name": "v0.1.1", "target_commitish": "old", "draft": True}]
        self.assertEqual(release_plan({"v0.1.0": "other"}, releases, "new"), {
            "version": "0.1.2", "reserved": False, "existing": False, "published": False,
        })

    def test_rerun_resumes_reserved_tag_and_draft(self):
        releases = [{"tag_name": "v0.1.1", "target_commitish": "main", "draft": True}]
        self.assertEqual(release_plan({"v0.1.1": "same"}, releases, "same"), {
            "version": "0.1.1", "reserved": True, "existing": True, "published": False,
        })
        self.assertEqual(release_plan({"v0.1.1": "same"}, [], "same"), {
            "version": "0.1.1", "reserved": True, "existing": False, "published": False,
        })

    def test_rerun_identifies_published_release_by_tag_commit(self):
        releases = [{"tag_name": "v0.1.0", "target_commitish": "main", "draft": False}]
        self.assertEqual(release_plan({"v0.1.0": "same", "v0.1.1": "other"}, releases, "same"), {
            "version": "0.1.0", "reserved": True, "existing": True, "published": True,
        })

    def test_concurrent_tag_reservation_retries_with_next_version(self):
        failed = subprocess.CompletedProcess([], 1, "", "Reference already exists")
        success = subprocess.CompletedProcess([], 0, "{}", "")
        with (
            patch("release_version.remote_tags", side_effect=[{}, {"v0.1.0": "other"}, {"v0.1.0": "other"}]),
            patch("release_version.remote_releases", return_value=[]),
            patch("release_version.subprocess.run", side_effect=[failed, success]) as run,
        ):
            result = reserve_release("owner/repo", "same")
            self.assertEqual(result["version"], "0.1.1")
            self.assertTrue(result["reserved"])
            self.assertIn("ref=refs/tags/v0.1.0", run.call_args_list[0].args[0])
            self.assertIn("ref=refs/tags/v0.1.1", run.call_args_list[1].args[0])

    def test_reservation_error_is_not_treated_as_conflict(self):
        failed = subprocess.CompletedProcess([], 1, "", "permission denied")
        with (
            patch("release_version.remote_tags", return_value={}),
            patch("release_version.remote_releases", return_value=[]),
            patch("release_version.subprocess.run", return_value=failed),
        ):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                reserve_release("owner/repo", "same")

    def test_remote_tags_use_peeled_commit_for_annotated_tags(self):
        output = (
            "object-sha\trefs/tags/v0.1.0\n"
            "commit-sha\trefs/tags/v0.1.0^{}\n"
            "other-sha\trefs/tags/v0.1.1\n"
        )
        with patch("release_version.subprocess.check_output", return_value=output):
            self.assertEqual(remote_tags(), {"v0.1.0": "commit-sha", "v0.1.1": "other-sha"})

    def test_rerun_reuses_existing_reservation_without_creating_tag(self):
        with (
            patch("release_version.remote_tags", return_value={"v0.1.0": "same"}),
            patch("release_version.remote_releases", return_value=[]),
            patch("release_version.subprocess.run") as run,
        ):
            self.assertEqual(reserve_release("owner/repo", "same")["version"], "0.1.0")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
