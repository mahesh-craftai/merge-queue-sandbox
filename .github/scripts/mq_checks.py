"""Sandbox stand-in for a repo's PR checks: title must be 10-72 chars."""


def check(pr, files, read_file):
    t = pr["title"]
    errors = [] if 10 <= len(t) <= 72 else [f"PR title must be 10-72 chars (got {len(t)})."]
    warnings = ["No README change."] if not any(f.endswith(".md") for f in files) else []
    return errors, warnings
