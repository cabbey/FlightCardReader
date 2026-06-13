"""
Exception hierarchy for the Flight Card Scanner application.
"""


class FlightCardScannerError(Exception):
    """Base exception for all Flight Card Scanner errors."""
    pass


class ConfigError(FlightCardScannerError):
    """Raised when configuration is missing, invalid, or unparseable."""
    pass


class ImageStorageError(FlightCardScannerError):
    """Raised when an image cannot be saved to or read from the Image Store."""
    pass


class ExtractionParseError(FlightCardScannerError):
    """Raised when the LLM response cannot be parsed as valid structured JSON.

    Attributes:
        raw_response: The raw response string from the LLM, preserved for logging.
    """

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.raw_response = raw_response


class OllamaUnavailableError(FlightCardScannerError):
    """Raised when an Ollama extraction endpoint is unreachable or returns an HTTP error."""
    pass


class DateResolutionError(FlightCardScannerError):
    """Raised when a flight date value cannot be resolved to a date within the Event Date Range."""
    pass
