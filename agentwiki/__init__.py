"""AgentWiki -- compile your own agent sessions into a personal knowledge wiki.

L0 (this package, today) is mechanical and free: it normalises every agent
session on this machine into one sqlite store and extracts the *skeleton* of
the knowledge graph with regex only.  No LLM is involved, so it is safe to run
repeatedly and cheap to re-run.

L1/L2 (claims extraction, page compilation) build on the same store; their
tables are already declared in db.py.
"""

__version__ = "0.1.0"
