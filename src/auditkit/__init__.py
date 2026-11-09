"""Agent Audit Kit: risk-based evaluation of conversational LLM agents.

The public API for packs. The core never imports a pack; a pack imports from here.
"""

from importlib.metadata import version

from auditkit.checks import CheckContext, check, failed, invalid, passed, tool
from auditkit.schema import Transcript, Turn
from auditkit.tools import ToolEnv

__version__ = version("agent-audit-kit")

__all__ = [
    "CheckContext",
    "ToolEnv",
    "Transcript",
    "Turn",
    "__version__",
    "check",
    "failed",
    "invalid",
    "passed",
    "tool",
]
