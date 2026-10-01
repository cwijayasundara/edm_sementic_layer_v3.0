"""Gateway errors: caller-safe messages with a stable machine-readable `code` (audited as error_code)."""
from prism.mcp.results import SourceError, UserFacingError

MAX_ECHO = 64  # longest caller-supplied name ever echoed back in a message


class GatewayError(UserFacingError):
    """A refusal or failure whose message is safe to show to the caller; `code` is stable."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code.replace("_", " "))


class GatewaySourceError(GatewayError, SourceError):
    """A source server refused the call (tool error); the message is the source's own, scrubbed."""

    def __init__(self, message: str):
        super().__init__("source_error", message)


def echo(value: object) -> str:
    """A caller-supplied name, safe to quote back: strings only, length-capped, repr-quoted."""
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    return repr(value if len(value) <= MAX_ECHO else value[:MAX_ECHO] + "...")


__all__ = ["GatewayError", "GatewaySourceError", "echo"]
