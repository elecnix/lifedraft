"""Resolve output file paths to a cache directory outside the repo.

Per DP#15, personal data never enters version control. CSV result files
containing financial simulation data belong in ~/.cache/lifedraft/,
not in the repo root where they could be accidentally committed.

Usage:
    from output_paths import output_path

    path = output_path("strategy_results.csv")
    df.to_csv(path, index=False)
"""

import os

CACHE_DIR = os.path.expanduser("~/.cache/lifedraft")


def output_path(filename: str, subdir: str = "") -> str:
    """Resolve an output filename to a path inside the cache directory.

    Creates the directory if it doesn't exist.

    Args:
        filename: The output filename (e.g. "strategy_results.csv").
        subdir: Optional subdirectory within the cache directory.

    Returns:
        Absolute path inside ~/.cache/lifedraft/.
    """
    directory = os.path.join(CACHE_DIR, subdir) if subdir else CACHE_DIR
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, filename)