"""Pre-publication scan: make sure nothing personal or secret is about to be
pushed to a public remote.

Run this from the project root before every `git push`:

    python tools/prepublish_scan.py

It is intentionally dumb (regex over the working tree) because the failure mode
it guards against -- a live credential or a real home directory embedded in a
committed file -- is dumb too, and usually arrives via a debug script or a
pasted log rather than via application code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist"}
SKIP_SUFFIX = {".pyc", ".db", ".sqlite", ".png", ".jpg", ".ico", ".zip", ".whl"}

# Anything that looks like a live credential.
SECRET_PATTERNS = [
    ("openai/deepseek key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("github pat", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github fine-grained", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{20,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password assign",
     re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*[\"']?[^\s\"']{8,}")),
]

# Machine-specific paths that would leak a real username or home layout.
# NOTE: the backslash runs are `\\+` / `[\\/]` on purpose.  A first version used
# `\\\\?` (four raw backslashes = "two literal backslashes") and therefore
# silently matched nothing in ordinary single-backslash paths.
PERSONAL_PATTERNS = [
    ("windows user profile",
     re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([A-Za-z0-9_.\-]+)")),
    ("unix home",
     re.compile(r"/(?:home|Users)/([A-Za-z0-9_.\-]+)")),
]

# Values that are fine / expected.  Matched against the *captured* group where
# the pattern has one, otherwise the whole match.
#
# Keep this list strictly generic.  Never add a real account name "because it is
# mine": that turns the scanner into a rubber stamp, which is worse than having
# no scanner at all, because you will trust it.
ALLOWLIST = {
    "me", "you", "user", "youruser", "username", "example", "name", "x",
    "<id>", "<user>", "<name>", "someuser", "testuser",
}

ALLOWED_SUBSTRINGS = (
    "[REDACTED",
    "REDACTED]",
    "AKIAIOSFODNN7EXAMPLE",   # AWS docs placeholder
    "sk-injection-test",       # intentional test fixture
)

# Account names that must always be flagged, even if they show up in a
# documentation example.  Populate via env (comma separated) so this file stays
# free of the very identifiers it is meant to protect: AGENTWIKI_PRIVATE_NAMES.
PRIVATE_NAMES = {
    s.strip().lower()
    for s in os.environ.get("AGENTWIKI_PRIVATE_NAMES", "").split(",")
    if s.strip()
}


def iter_files(root: Path):
    """Yield the files git would actually publish.

    Scanning the raw working tree gives false alarms from gitignored scratch
    (recon dumps, local databases).  What matters before a push is the set of
    *tracked* files, so prefer `git ls-files` and fall back to a walk.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root), capture_output=True, check=True,
        ).stdout
        tracked = [f for f in out.decode("utf-8", "replace").split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        tracked = None

    if tracked is not None:
        for rel in tracked:
            p = root / rel
            if p.suffix.lower() in SKIP_SUFFIX or not p.is_file():
                continue
            yield p
        return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in SKIP_SUFFIX:
                continue
            yield p


def scan(root: Path) -> int:
    findings = 0
    n_files = 0

    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n_files += 1
        rel = path.relative_to(root)

        for lineno, line in enumerate(text.splitlines(), 1):
            if any(s in line for s in ALLOWED_SUBSTRINGS):
                continue
            for label, pat in SECRET_PATTERNS:
                m = pat.search(line)
                if m:
                    findings += 1
                    s = m.group(0)
                    print(f"  SECRET  {rel}:{lineno}  [{label}]  {s[:6]}...{s[-4:]}")
            for label, pat in PERSONAL_PATTERNS:
                for m in pat.finditer(line):
                    who = m.group(1) if m.groups() else m.group(0)
                    low = who.lower()
                    # An explicitly private name always wins over the allowlist:
                    # someone can put their real account in a doc example by
                    # accident, and that is precisely what we must catch.
                    if low in PRIVATE_NAMES:
                        findings += 1
                        print(f"  PRIVATE  {rel}:{lineno}  [{label}]  {m.group(0)}")
                        continue
                    if low in ALLOWLIST:
                        continue
                    findings += 1
                    print(f"  PERSONAL {rel}:{lineno}  [{label}]  {m.group(0)}")

    print(f"\n  scanned {n_files} files -> {findings} finding(s)")
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    print(f"=== pre-publish scan: {root} ===")
    findings = scan(root)
    if findings:
        print("\nBLOCKED: resolve the findings above before publishing.")
        return 1
    print("\nCLEAN: no secrets or personal paths found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
