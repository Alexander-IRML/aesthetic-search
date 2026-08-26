class SearchApiError(RuntimeError):
    """Base class for failures safe to map to the public API contract."""


class InvalidUploadError(SearchApiError):
    """The request did not contain a decodable supported image."""


class UploadTooLargeError(InvalidUploadError):
    """The upload exceeded the configured byte or pixel limit."""


class UnsupportedUploadError(InvalidUploadError):
    """The upload used an unsupported media type or image format."""


class SearchBusyError(SearchApiError):
    """The bounded inference worker is already occupied."""


class SearchTimeoutError(SearchApiError):
    """Query encoding exceeded its public latency budget."""


class CatalogConsistencyError(SearchApiError):
    """The serving index references missing catalog or image metadata."""


class FeedbackUnavailableError(SearchApiError):
    """Durable product telemetry is unavailable for feedback writes."""


class FeedbackTargetNotFoundError(SearchApiError):
    """The rated artwork was not shown by this query and browser session."""
