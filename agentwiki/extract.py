"""L0 entity extraction: mechanical, regex-only, zero LLM cost.

Extracts the *skeleton* of the knowledge graph from raw text. Semantic
concepts (e.g. "linear attention") are deliberately NOT attempted here --
that is L1's job with an LLM. This layer only pulls out things that are
lexically unambiguous: repos, PRs, file paths, filenames, domains,
error classes, packages, and tool names.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

MAX_ENTITIES_PER_ITEM = 60

# ---------------------------------------------------------------- patterns

RE_REPO = re.compile(
    r"\b(?:github|gitee|gitlab|bitbucket)\.com[/:]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)
RE_PR = re.compile(r"\b(?:pull|pulls|merge_requests|pull-request)/(\d{1,7})\b")
RE_ISSUE = re.compile(r"\b(?:issues|issue)/(\d{1,7})\b")
RE_URL = re.compile(r"https?://([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.IGNORECASE)
RE_WINPATH = re.compile(r"\b([A-Za-z]:\\[^\s\"'<>|*?\r\n\t]{2,200})")
RE_VERBATIM_PATH = re.compile(r"\\\\\?\\[A-Za-z]:\\[^\s\"'<>|*?\r\n\t]{2,200}")
RE_UNIXPATH = re.compile(r"(?<![\w.])/(?:home|root|usr|opt|etc|var|tmp|mnt)/[^\s\"'<>|*?\r\n\t]{2,200}")
RE_FILE = re.compile(
    r"\b([A-Za-z0-9_\-]{1,80}\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|c|h|cc|cpp|hpp|cxx|"
    r"java|kt|rb|php|swift|cs|scala|sql|md|rst|json|jsonl|toml|ini|cfg|yaml|yml|xml|"
    r"sh|bash|zsh|ps1|psm1|bat|cmd|dockerfile|makefile|cmake|gradle|lua|proto|tf))\b",
    re.IGNORECASE,
)
RE_ERR = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,40}(?:Error|Exception|Warning|Fault))\b")
RE_PIP = re.compile(r"\bpip3?\s+install\s+(?:-U\s+|--upgrade\s+|-e\s+)?([A-Za-z0-9_.\-]{2,60})")
RE_NPM = re.compile(r"\bnpm\s+(?:i|install|add)\s+(?:-g\s+|--save\s+|-D\s+)?(@?[A-Za-z0-9_./@\-]{2,60})")
RE_TOOL = re.compile(r'"name"\s*:\s*"([a-z_][a-z0-9_]{1,30})"')

# noise we never want as entities
FILE_STOP = {
    "test.py", "main.py", "__init__.py", "setup.py", "index.js", "index.ts",
    "package.json", "package-lock.json", "tsconfig.json", "readme.md",
    "license", "makefile", "dockerfile", "config.json", "requirements.txt",
}

# Segments that are *containers*, never the project itself.  We skip past them
# so `C:\Users\me\proj\src\a.py` resolves to the project, not to `C:\Users`.
_CONTAINER_SEGMENTS = {
    "users", "home", "documents", "onedrive", "desktop", "downloads",
    "appdata", "local", "roaming", "temp", "tmp",
}

# System / toolchain trees: never interesting as a "project".
_SYS_SEGMENTS = {
    "windows", "system32", "syswow64", "program files", "program files (x86)",
    "programdata", "usr", "etc", "var", "opt", "bin", "sbin", "lib", "lib64",
    "include", "node_modules", "site-packages", "dist-packages", ".git",
    ".venv", "venv", "__pycache__",
}

# Builtin exception names carry no information about *your* problem: every
# Python codebase raises ValueError.  Keeping them drowns the signal, so the
# "top errors" table only shows project-specific ones (CUDA*, ONNX*, ...).
GENERIC_ERRORS = {
    "ValueError", "TypeError", "RuntimeError", "AssertionError", "ImportError",
    "ModuleNotFoundError", "KeyError", "IndexError", "AttributeError",
    "NameError", "ZeroDivisionError", "FileNotFoundError", "PermissionError",
    "OSError", "IOError", "StopIteration", "NotImplementedError", "TimeoutError",
    "UnicodeDecodeError", "UnicodeEncodeError", "JSONDecodeError", "SystemExit",
    "Exception", "BaseException", "Error", "Warning", "DeprecationWarning",
    "UserWarning", "RuntimeWarning", "ConnectionError", "ConnectionResetError",
    "ConnectionRefusedError", "ConnectionAbortedError", "MemoryError",
    "OverflowError", "RecursionError", "SyntaxError", "IndentationError",
    "TabError", "UnboundLocalError", "EOFError", "BrokenPipeError",
    "InterruptedError", "IsADirectoryError", "NotADirectoryError",
    "ChildProcessError", "BlockingIOError", "ArithmeticError", "LookupError",
    "BufferError", "ReferenceError", "GeneratorExit", "FloatingPointError",
    "FileExistsError", "ProcessLookupError", "KeyboardInterrupt",
}

_VERBATIM = re.compile(r"^\\\\\?\\|^//\?/")
_DRIVE = re.compile(r"^[A-Za-z]:$")
_USERLIKE = re.compile(r"^[A-Za-z]?\d{3,}$")
# Reverse-domain vendor dirs (`com.xlang.xharness`, `io.github.foo`) are hosts,
# never the project itself.
_DOMAINLIKE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]*\.[A-Za-z]")
# Generic sub-directories: `D:\proj\tests` is project `proj`, not `tests`.
_SUBDIR_NOISE = {
    "tests", "test", "src", "lib", "libs", "docs", "doc", "examples", "example",
    "scripts", "script", "bench", "benchmarks", "tools", "tool", "utils",
    "build", "dist", "out", "output", "target", "obj", "bin", "cmd", "internal",
    "pkg", "apps", "app", "include", "config", "conf", "assets", "static",
    "public", "templates", "migrations", "fixtures", "samples", "sample",
    "demo", "main", "core", "common", "server", "client", "web", "api",
}


def norm_name(s: str) -> str:
    """Canonical entity id: lowercased, separator-normalised."""
    s = s.strip().strip(".,;:)\"'`]}>")
    s = s.lower()
    s = s.replace("\\", "/")
    s = re.sub(r"/{2,}", "/", s)
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff/._\-:@+#]", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:180]


def make_eid(kind: str, name: str) -> str:
    return f"{kind}:{norm_name(name)}"


def _clean_path(p: str) -> str:
    """Strip the Windows verbatim prefix (`\\\\?\\`) and normalise separators."""
    p = _VERBATIM.sub("", p)
    p = p.replace("\\", "/")
    p = re.sub(r"/{2,}", "/", p)
    return p


def project_root(path: str) -> str | None:
    """`C:\\Users\\me\\proj\\src\\a.py` -> `c:/users/me/proj` (the project, not the file).

    Container segments (Users, OneDrive, AppData, ...) are skipped, and
    system/toolchain trees are rejected outright, so this yields directories
    you would actually recognise as "a project I worked in".
    """
    p = _clean_path(path).strip("/")
    parts = [x for x in p.split("/") if x and x not in (".", "..")]
    if not parts:
        return None

    if _DRIVE.match(parts[0]):
        rest = parts[1:]
        if not rest:
            return None
        i = 0
        while i < len(rest) - 1 and (
            rest[i].lower() in _CONTAINER_SEGMENTS
            or _USERLIKE.match(rest[i])
            or _DOMAINLIKE.match(rest[i])
        ):
            i += 1
        seg = rest[i]
        if seg.lower() in _SYS_SEGMENTS or _USERLIKE.match(seg):
            return None
        base = parts[0] + "/" + seg
    else:
        if parts[0].lower() in _SYS_SEGMENTS:
            return None
        if len(parts) < 3:
            return None
        base = "/".join(parts[:3])

    low = [s.lower() for s in base.split("/")]
    if any(s in _SYS_SEGMENTS for s in low[1:]):
        return None
    return norm_name(base)[:120] or None


def repo_from_cwd(cwd: str | None) -> str | None:
    """Best-effort project name from a working directory.

    Used by sources that have no `git_origin_url` (xharness, claude).  Returns
    a bare directory name such as `rwkv7-hf-adapter`, or None.
    """
    if not cwd:
        return None
    p = _clean_path(cwd).strip("/")
    parts = [x for x in p.split("/") if x and x not in (".", "..")]
    if not parts:
        return None
    for seg in reversed(parts):
        low = seg.lower()
        if low in _CONTAINER_SEGMENTS or low in _SYS_SEGMENTS or _USERLIKE.match(seg):
            continue
        if _DRIVE.match(seg) or _DOMAINLIKE.match(seg) or low in _SUBDIR_NOISE:
            continue
        return norm_name(seg)[:120] or None
    return None


def extract(text: str) -> Counter:
    """Return Counter of (kind, display_name) -> count for one text blob."""
    out: Counter = Counter()
    if not text:
        return out

    for owner, repo in RE_REPO.findall(text):
        repo = re.sub(r"\.git$", "", repo, flags=re.IGNORECASE)
        if repo.lower() in ("blob", "tree", "raw", "pull", "issues"):
            continue
        out[("repo", f"{owner}/{repo}")] += 1

    for n in RE_PR.findall(text):
        out[("pr", f"#{n}")] += 1
    for n in RE_ISSUE.findall(text):
        out[("issue", f"#{n}")] += 1

    for dom in RE_URL.findall(text):
        out[("domain", dom.lower())] += 1

    seen_paths: set[str] = set()
    for pat in (RE_VERBATIM_PATH, RE_WINPATH, RE_UNIXPATH):
        for p in pat.findall(text):
            pl = _clean_path(p).lower()
            if pl in seen_paths:
                continue
            seen_paths.add(pl)
            root = project_root(p)
            if root:
                out[("path", root)] += 1

    for f in RE_FILE.findall(text):
        fl = f.lower()
        if fl in FILE_STOP:
            continue
        out[("file", fl)] += 1

    for e in RE_ERR.findall(text):
        if e in GENERIC_ERRORS:
            continue
        out[("error", e)] += 1

    for p in RE_PIP.findall(text):
        if p.lower() not in ("install", "upgrade"):
            out[("pkg", p)] += 1
    for p in RE_NPM.findall(text):
        if p not in ("-", "."):
            out[("pkg", p)] += 1

    for t in RE_TOOL.findall(text):
        if t in ("pwsh", "bash", "read", "write", "edit", "grep", "glob",
                 "web_search", "web_fetch", "agent", "job_output"):
            out[("tool", t)] += 1

    return out


def top_n(counter: Counter, n: int = MAX_ENTITIES_PER_ITEM) -> list:
    return [kv for kv, _ in counter.most_common(n)]


def iter_entities(text: str) -> Iterable[tuple]:
    """Yield (eid, kind, display_name, count) capped per item."""
    c = extract(text)
    for (kind, display), count in c.most_common(MAX_ENTITIES_PER_ITEM):
        if not display or len(display) < 2:
            continue
        yield make_eid(kind, display), kind, display[:300], count
