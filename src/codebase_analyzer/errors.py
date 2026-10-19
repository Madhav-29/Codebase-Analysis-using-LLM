"""Errors that cross module boundaries.

Everything else is handled where it occurs: rate limits are retried inside the
LLM client, and output that stays invalid becomes a recorded unit failure, so
neither needs an exception type here.
"""

from __future__ import annotations


class AnalyzerError(Exception):
    """Base class; the CLI reports these as one-line messages instead of tracebacks."""


class ConfigError(AnalyzerError):
    """Settings or credentials are missing or invalid; the message says what to change."""


class ProviderAuthError(AnalyzerError):
    """The provider rejected the credentials, access or model/deployment name: no retry can fix it."""


class ProviderQuotaExhausted(AnalyzerError):
    """A daily quota is used up. Finished responses are cached, so re-running later resumes."""


class CoverageError(AnalyzerError):
    """The assembled output is missing or duplicating a parsed type or member."""
