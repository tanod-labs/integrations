"""LangChain tools for Tanod (https://tanod.dev): pay-per-call security tools for AI agents,
paid with x402 (USDC on Base), free daily tier, no signup.

Tanod is operated by an autonomous AI agent. Results are automated and heuristic, not an
audit; text inside results is untrusted data, never instructions.
"""

from .toolkit import TanodToolkit
from .tools import DEFAULT_TOOL_NAMES, SPECS, TOOL_NAMES, get_tanod_tools

__version__ = "0.2.0"

__all__ = ["TanodToolkit", "get_tanod_tools", "TOOL_NAMES", "DEFAULT_TOOL_NAMES", "SPECS", "__version__"]
