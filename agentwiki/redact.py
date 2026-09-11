"""Secret scrubbing.

Every byte that enters the wiki passes through ``scrub()``.  We would rather
over-redact a token inside a code snippet than leak a live credential into a
searchable index that an LLM will later read.

Two hard-won details:

1. **Do not use ``\\b`` to anchor token prefixes.**  Python's ``re`` treats CJK
   characters as word characters, so in ``嗯sk-abcdef...`` there is no word
   boundary between ``嗯`` and ``s`` and a ``\\bsk-`` pattern silently fails to
   match.  We use an explicit ASCII lookbehind instead.
2. Scrub *everything* you persist, including any raw/original copy of the
   record, not just the extracted text.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# A token must not be glued to a preceding ASCII word character.  CJK (or any
# other non-ASCII) text before a key is extremely common in these transcripts.
_NOT_ALNUM = r"(?<![A-Za-z0-9_])"

_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED_PEM]"),

    # OpenAI / DeepSeek / Moonshot style: sk-..., sk-proj-..., sk-or-v1-...
    (re.compile(_NOT_ALNUM + r"sk-[A-Za-z0-9_\-]{12,}"), "[REDACTED_KEY]"),
    # generic 32+ char hex/base64-ish secrets assigned to nothing in particular
    (re.compile(_NOT_ALNUM + r"(?:dsk|ds)-[A-Za-z0-9]{16,}"), "[REDACTED_KEY]"),

    (re.compile(_NOT_ALNUM + r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED_GH]"),
    (re.compile(_NOT_ALNUM + r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GH]"),
    (re.compile(_NOT_ALNUM + r"AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"), "[REDACTED_AWS]"),
    (re.compile(_NOT_ALNUM + r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_SLACK]"),
    # HuggingFace / GitLab / generic provider prefixes
    (re.compile(_NOT_ALNUM + r"hf_[A-Za-z0-9]{20,}"), "[REDACTED_HF]"),
    (re.compile(_NOT_ALNUM + r"glpat-[A-Za-z0-9_\-]{16,}"), "[REDACTED_GL]"),

    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{16,}"), "Bearer [REDACTED]"),

    # JWT
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
     "[REDACTED_JWT]"),

    # key=value / key: value
    (re.compile(r"(?i)" + _NOT_ALNUM +
                r"(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|"
                r"secret[_-]?key|secret|password|passwd|pwd)"
                r"(?![A-Za-z0-9_])"
                r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.\+/=]{12,})[\"']?"),
     r"\1=[REDACTED]"),
]


def scrub(text: str) -> str:
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
