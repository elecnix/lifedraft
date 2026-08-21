"""Tests for output_paths module (DP#15: personal data never enters version control).

Verifies that output files are resolved to ~/.cache/lifedraft/ rather
than the repo root, preventing accidental commits of personal financial data.
"""

import os
import tempfile

import pytest

from output_paths import CACHE_DIR, output_path


class TestOutputPath:
    """output_path resolves filenames to the cache directory."""

    def test_resolves_simple_filename(self):
        """A bare filename resolves to ~/.cache/lifedraft/<filename>."""
        result = output_path("strategy_results.csv")
        expected = os.path.join(CACHE_DIR, "strategy_results.csv")
        assert result == expected

    def test_resolves_with_subdir(self):
        """A filename with subdir resolves to ~/.cache/lifedraft/<subdir>/<filename>."""
        result = output_path("monte_carlo_results.csv", subdir="sensitivity")
        expected = os.path.join(CACHE_DIR, "sensitivity", "monte_carlo_results.csv")
        assert result == expected

    def test_creates_cache_directory(self):
        """output_path creates the target directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = os.path.join(tmpdir, "lifedraft")
            # Patch CACHE_DIR for this test
            import output_paths
            original = output_paths.CACHE_DIR
            output_paths.CACHE_DIR = target_dir
            try:
                result = output_path("test.csv")
                assert os.path.isdir(target_dir)
                assert result == os.path.join(target_dir, "test.csv")
            finally:
                output_paths.CACHE_DIR = original

    def test_creates_subdirectory(self):
        """output_path creates subdirectories within the cache directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = os.path.join(tmpdir, "lifedraft")
            import output_paths
            original = output_paths.CACHE_DIR
            output_paths.CACHE_DIR = target_dir
            try:
                result = output_path("test.csv", subdir="reports")
                assert os.path.isdir(os.path.join(target_dir, "reports"))
                assert result == os.path.join(target_dir, "reports", "test.csv")
            finally:
                output_paths.CACHE_DIR = original

    def test_cache_dir_is_outside_repo(self):
        """CACHE_DIR is ~/.cache/lifedraft/, not inside any repo."""
        assert ".cache" in CACHE_DIR
        assert "lifedraft" in CACHE_DIR
        # It should NOT be under a Source directory (which contains repos)
        assert "/Source/" not in CACHE_DIR

    def test_existing_directory_no_error(self):
        """Calling output_path twice doesn't raise an error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            import output_paths
            original = output_paths.CACHE_DIR
            output_paths.CACHE_DIR = tmpdir
            try:
                result1 = output_path("first.csv")
                result2 = output_path("second.csv")
                assert os.path.isdir(tmpdir)
                assert result1 != result2
            finally:
                output_paths.CACHE_DIR = original