"""``TanodToolkit``: all Tanod tools as a LangChain toolkit."""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.tools import BaseTool, BaseToolkit
from pydantic import ConfigDict

from tanod import AsyncTanod, Tanod

from .tools import get_tanod_tools


class TanodToolkit(BaseToolkit):
    """Tanod tools (txpeek, pactlint, toolsniff, sitepeek, dnspeek, chainpeek, agentscan).

    Example:
        >>> toolkit = TanodToolkit(client=Tanod(max_price_usd=0.05))
        >>> tools = toolkit.get_tools()
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: Optional[Tanod] = None
    async_client: Optional[AsyncTanod] = None
    include: Optional[list[str]] = None

    def get_tools(self) -> list[BaseTool]:
        kwargs: dict[str, Any] = {}
        return get_tanod_tools(self.client, async_client=self.async_client, include=self.include, **kwargs)
