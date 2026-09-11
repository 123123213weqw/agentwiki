"""Per-source ingesters.  Each exposes `ingest(conn, **opts) -> dict`."""

from . import claude, codex, xharness  # noqa: F401

ALL = {
    "codex": codex,
    "claude": claude,
    "xharness": xharness,
}
