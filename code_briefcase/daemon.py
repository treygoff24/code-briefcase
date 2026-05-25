"""
Socket-based daemon that holds indexes in memory.

This module is a backwards-compatibility wrapper. The actual implementation
has been modularized into the code_briefcase.daemon package:
  - code_briefcase.daemon.core: TLDRDaemon class
  - code_briefcase.daemon.startup: start_daemon, stop_daemon, query_daemon
  - code_briefcase.daemon.cached_queries: @salsa_query cached functions

For new code, import directly from code_briefcase.daemon:
    from code_briefcase.daemon import TLDRDaemon, start_daemon, query_daemon
"""

# Re-export everything for backwards compatibility
from code_briefcase.daemon import (
    IDLE_TIMEOUT,
    DaemonResponse,
    TLDRDaemon,
    cached_architecture,
    cached_cfg,
    cached_context,
    cached_dead_code,
    cached_dfg,
    cached_extract,
    cached_importers,
    cached_imports,
    cached_search,
    cached_slice,
    cached_structure,
    cached_tree,
    main,
    query_daemon,
    query_daemon_response,
    query_or_start_daemon,
    start_daemon,
    stop_daemon,
)

__all__ = [
    "TLDRDaemon",
    "IDLE_TIMEOUT",
    "start_daemon",
    "stop_daemon",
    "query_daemon",
    "query_daemon_response",
    "query_or_start_daemon",
    "DaemonResponse",
    "main",
    "cached_search",
    "cached_extract",
    "cached_dead_code",
    "cached_architecture",
    "cached_cfg",
    "cached_dfg",
    "cached_slice",
    "cached_tree",
    "cached_structure",
    "cached_context",
    "cached_imports",
    "cached_importers",
]

if __name__ == "__main__":
    main()
