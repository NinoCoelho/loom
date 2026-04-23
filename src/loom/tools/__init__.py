"""Tool system — handlers, registry, and built-in tool implementations.

:class:`ToolHandler` is the abstract base for all tool backends; each
handler describes itself via a :class:`~loom.types.ToolSpec` and returns
a :class:`ToolResult` when invoked. :class:`ToolRegistry` maintains the
active handler index and dispatches calls by name.

Built-in tools include:

* :class:`DelegateTool` — delegates a task to another agent.
* :class:`EditIdentityTool` — edits SOUL.md, IDENTITY.md, or USER.md files.
* :class:`BrokerAskUserTool` — human-in-the-loop ask (via :mod:`loom.hitl`).
* :class:`~loom.tools.ssh.SshCallTool` — SSH command execution.
* :class:`~loom.tools.vault.VaultToolHandler` — vault file operations.
* :class:`~loom.tools.search.WebSearchTool` — multi-provider web search.
* :class:`~loom.tools.scrape.WebScrapeTool` — web page scraping with format conversion.
"""

from loom.tools.base import ToolHandler as ToolHandler
from loom.tools.base import ToolResult as ToolResult
from loom.tools.delegate import DelegateTool as DelegateTool
from loom.tools.profile import EditIdentityTool as EditIdentityTool
from loom.tools.registry import ToolRegistry as ToolRegistry
from loom.tools.scrape import WebScrapeTool as WebScrapeTool
from loom.tools.search import WebSearchTool as WebSearchTool

__all__ = [
    "DelegateTool",
    "EditIdentityTool",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "WebScrapeTool",
    "WebSearchTool",
]
