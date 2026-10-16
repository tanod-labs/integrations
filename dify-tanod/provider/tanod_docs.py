from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from utils.tanod import TanodError, check_key, parse_max_price


class TanodDocsProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        """Offline checks only: no network call and no payment while saving credentials."""
        try:
            parse_max_price(credentials.get("max_price_usd"))
            if (credentials.get("wallet_private_key") or "").strip():
                check_key(credentials["wallet_private_key"])
        except TanodError as e:
            raise ToolProviderCredentialValidationError(str(e))
