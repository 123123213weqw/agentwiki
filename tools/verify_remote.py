"""Verify what is ACTUALLY public on the remote.

Trusting the local commit is not enough: a pre-push hook, a mis-set remote or a
wrong branch would all push something different from what we scanned.  This
fetches every blob from the published branch and re-runs the same patterns.

    python tools/verify_remote.py [owner/repo] [branch]
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_scanner():
    spec = importlib.util.spec_from_file_location(
        "prepublish_scan", HERE / "prepublish_scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gh(*args: str) -> str:
    """Run gh and decode as UTF-8.

    Windows defaults subprocess text mode to the ANSI code page (GBK here),
    which explodes on source files containing non-ASCII prose.
    """
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "123123213weqw/agentwiki"
    branch = sys.argv[2] if len(sys.argv) > 2 else "main"
    ps = load_scanner()

    paths = [
        p
        for p in gh(
            "api",
            f"repos/{repo}/git/trees/{branch}?recursive=1",
            "--jq",
            '.tree[] | select(.type=="blob") | .path',
        ).splitlines()
        if p
    ]

    print(f"=== verifying {repo}@{branch}: {len(paths)} published files ===")

    findings = 0
    scanned = 0
    for path in paths:
        if Path(path).suffix.lower() in ps.SKIP_SUFFIX:
            continue
        try:
            text = gh("api", f"repos/{repo}/contents/{path}",
                      "-H", "Accept: application/vnd.github.raw")
        except subprocess.CalledProcessError as exc:
            print(f"  !! could not fetch {path}: {exc}")
            findings += 1
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(s in line for s in ps.ALLOWED_SUBSTRINGS):
                continue
            for label, pat in ps.SECRET_PATTERNS:
                m = pat.search(line)
                if m:
                    findings += 1
                    print(f"  SECRET   {path}:{lineno} [{label}]")
            for label, pat in ps.PERSONAL_PATTERNS:
                for m in pat.finditer(line):
                    who = (m.group(1) if m.groups() else m.group(0)).lower()
                    if who in ps.ALLOWLIST and who not in ps.PRIVATE_NAMES:
                        continue
                    findings += 1
                    print(f"  PERSONAL {path}:{lineno} [{label}] {m.group(0)}")

    print(f"\n  scanned {scanned} published files -> {findings} finding(s)")
    if findings:
        print("\nFAIL: the public remote contains sensitive content.")
        return 1
    print("\nPASS: the published tree is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
