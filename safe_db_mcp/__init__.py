"""SafeDataBaseMCP: a database behind a validated MCP tool surface.

The package is layered so the safety rules can be read and tested on their own:

* :mod:`safe_db_mcp.validation` - the allowed SQL grammar and every refusal;
* :mod:`safe_db_mcp.proposals` - the single-use, expiring pending-change store;
* :mod:`safe_db_mcp.database` - connections (read-only and read/write) and seeding;
* :mod:`safe_db_mcp.engine` - the operations as plain Python;
* :mod:`safe_db_mcp.server` - the MCP adapter over those operations.
"""

from .engine import ProposalError, SafeDatabase, SqlRejected
from .server import build_server

__version__ = "1.0.0"

__all__ = ["ProposalError", "SafeDatabase", "SqlRejected", "__version__", "build_server"]
