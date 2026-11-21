class GuardrailViolation(Exception):
    """Raised when a guardrail blocks an input, tool call or action.

    `code` is a stable machine-readable string the frontend can switch on.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
