"""Audit that every CSS class the UI emits actually has a rule.

Why this exists
---------------
`app.js` and `styles.css` are maintained by hand and can drift apart silently:
the page still renders, so nothing looks broken, but elements lose their layout
(the stat cards stack vertically instead of forming a grid, and nothing warns
you).  This script makes that class of bug loud.

Three subtleties it has to get right, each of which produced a wrong answer in
an earlier version:

1. `styles.css` groups selectors (`.snip, .snip-meta { ... }`) and nests them
   (`.card h2 { ... }`), so scanning only line-start tokens reports live classes
   as dead.
2. `app.js` builds class names dynamically: `class="kbadge k-${kind}"`.  The
   literal text contains `k-`, which is not a class anyone should style.  After
   removing the `${...}` expressions, a bare token ending in `-` is exactly such
   a prefix -- that is the rule used here.
3. Interpolated expressions contain quotes and operators.  Splitting the raw
   attribute on whitespace therefore yields junk tokens like `?` and `' active'`,
   which is why the `${...}` must be stripped *before* splitting.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"

# What each dynamic prefix can actually produce, so the concrete names can be
# checked against the stylesheet instead of just the prefix.
DYNAMIC_PREFIXES = {
    "k-": ["repo", "path", "error", "pr", "issue", "pkg", "domain", "file"],
    "src-": ["codex", "claude", "xharness"],
}

CLASS_ATTR = re.compile(r"""class\s*=\s*(?:"([^"]*)"|'([^']*)')""")
CSS_CLASS = re.compile(r"\.([A-Za-z][\w-]*)")
VALID_CLASS = re.compile(r"^[A-Za-z][\w-]*$")


def strip_interpolations(value: str) -> str:
    """Remove `${ ... }` spans, honouring nested braces.

    Replacing them with a space keeps surrounding tokens separate, so
    `a${x}b` yields `a b` rather than the invalid `ab`.
    """
    out = []
    depth = 0
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "$" and i + 1 < len(value) and value[i + 1] == "{":
            depth = 1
            i += 2
            while i < len(value) and depth:
                if value[i] == "{":
                    depth += 1
                elif value[i] == "}":
                    depth -= 1
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_classes(value: str) -> tuple[list[str], list[str]]:
    """`kbadge k-${kind}` -> (['kbadge'], ['k-'])."""
    literals, prefixes = [], []
    for tok in strip_interpolations(value or "").split():
        if tok.endswith("-") and VALID_CLASS.match(tok):
            prefixes.append(tok)
        elif VALID_CLASS.match(tok):
            literals.append(tok)
    return literals, prefixes


def emitted_classes() -> tuple[dict[str, int], dict[str, int]]:
    used: dict[str, int] = {}
    prefixes: dict[str, int] = {}
    for name in ("app.js", "index.html"):
        path = WEB / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in CLASS_ATTR.finditer(text):
            value = m.group(1) if m.group(1) is not None else m.group(2)
            lits, prefs = split_classes(value)
            for c in lits:
                used[c] = used.get(c, 0) + 1
            for p in prefs:
                prefixes[p] = prefixes.get(p, 0) + 1
    return used, prefixes


def defined_classes() -> set[str]:
    css = (WEB / "styles.css").read_text(encoding="utf-8", errors="replace")
    # Strip comments first: prose mentions like "app.js" would otherwise be read
    # as classes `js` / `py` and reported as dead rules.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # Drop declaration bodies so decimal values (`.5em`) are not read as classes.
    css = re.sub(r"\{[^{}]*\}", "{}", css)
    return set(CSS_CLASS.findall(css))


def main() -> int:
    used, prefixes = emitted_classes()
    defined = defined_classes()

    unknown_prefixes = []
    for prefix, count in prefixes.items():
        variants = DYNAMIC_PREFIXES.get(prefix)
        if variants is None:
            # Unknown prefix: accept it if the stylesheet styles *something* in
            # that namespace, otherwise it is a real gap.
            if not any(d.startswith(prefix) for d in defined):
                unknown_prefixes.append(prefix)
            continue
        for v in variants:
            used.setdefault(prefix + v, count)

    print(f"=== classes emitted by the UI: {len(used)}")
    print(f"=== classes defined in CSS:   {len(defined)}")

    stale = sorted(c for c in used if c not in defined)
    dead = sorted(c for c in defined if c not in used)

    if stale:
        print(f"\nUNSTYLED ({len(stale)}) -- emitted but no CSS rule exists:")
        for c in stale:
            print(f"   {c}   (used {used[c]}x)")
    if unknown_prefixes:
        print(f"\nUNRESOLVED PREFIXES ({len(unknown_prefixes)}): "
              + " ".join(unknown_prefixes))
    if dead:
        print(f"\nDEAD CSS ({len(dead)}) -- rule exists but nothing emits it:")
        print("   " + " ".join(dead))

    if stale or unknown_prefixes:
        print("\nFAIL: the UI will render unstyled elements.")
        return 1
    print("\nOK: every emitted class has a rule.")
    if dead:
        print("(dead rules are cosmetic only, but worth pruning)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
