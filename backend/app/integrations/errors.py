class IntegrationError(RuntimeError):
    """An external system (Shopify, Freshdesk) failed or is unreachable."""


class IntegrationAccessDenied(IntegrationError):
    """Credentials/scopes are wrong or missing (401/403, ACCESS_DENIED). Retrying cannot help: alert a human."""
