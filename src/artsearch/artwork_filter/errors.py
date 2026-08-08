from __future__ import annotations


class ArtworkFilterError(Exception):
    """Base class for typed artwork-filter failures."""


class DownloadError(ArtworkFilterError):
    pass


class DownloadTooLargeError(DownloadError):
    pass


class UnsupportedMediaError(ArtworkFilterError):
    pass


class ImageDecodeError(ArtworkFilterError):
    pass


class ImageValidationError(ArtworkFilterError):
    pass


class ModelInferenceError(ArtworkFilterError):
    pass


class ModelArtifactError(ArtworkFilterError):
    pass


class PersistenceError(ArtworkFilterError):
    pass


class CandidateInputError(ArtworkFilterError):
    pass


class RoutingError(ArtworkFilterError):
    pass
