#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 工作流静态安全策略与供应链回归检查"""

import os
import re
import unittest

WORKFLOW_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "renew.yml")
)


class TestWorkflowPolicy(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.exists(WORKFLOW_PATH), f"Workflow file not found at {WORKFLOW_PATH}")
        with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_minimal_permissions_declared(self):
        self.assertIn("permissions:", self.content)
        self.assertIn("contents: read", self.content)

    def test_concurrency_group_declared(self):
        self.assertIn("concurrency:", self.content)
        self.assertIn("cancel-in-progress: false", self.content)

    def test_actions_pinned_to_immutable_commit_hashes(self):
        # Disallow mutable tags like @v4 or @v5 without SHA
        unpinned_patterns = [
            r"uses:\s+actions/checkout@v\d",
            r"uses:\s+actions/setup-python@v\d",
            r"uses:\s+actions/upload-artifact@v\d",
        ]
        for pattern in unpinned_patterns:
            self.assertIsNone(
                re.search(pattern, self.content),
                f"Found unpinned action matching {pattern}"
            )

        # Must have full 40-character hex commit SHA
        sha_pattern = r"uses:\s+actions/[\w-]+@[0-9a-fA-F]{40}"
        matches = re.findall(sha_pattern, self.content)
        self.assertGreaterEqual(len(matches), 3, "Expected at least 3 actions pinned to 40-char SHA")

    def test_singbox_integrity_and_fail_closed(self):
        self.assertIn("SINGBOX_SHA256=", self.content)
        self.assertIn("sha256sum --check", self.content)
        self.assertIn("Fail-Closed", self.content)

    def test_no_sensitive_renew_state_in_artifacts(self):
        self.assertNotIn("renew_state.json", self.content)


if __name__ == "__main__":
    unittest.main()
