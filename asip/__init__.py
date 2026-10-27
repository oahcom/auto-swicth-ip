"""Reusable building blocks for auto-switch-ip.

The daemon started as one 5k-line module. These modules hold the parts that
have no dependency on daemon globals, so the daemon, the operator CLIs and the
self-check timer can share one implementation instead of four copies.

Nothing here imports ``config`` or ``daemon`` — callers pass the paths,
endpoints and secrets explicitly. That keeps the dependency direction one-way
and makes each helper testable without a running sing-box.
"""

from . import atomic_io, clash_client, egress, redact

__all__ = ["atomic_io", "clash_client", "egress", "redact"]
