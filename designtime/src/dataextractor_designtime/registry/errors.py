"""Registry errors. Each carries the HTTP status the API surfaces it as."""

from __future__ import annotations


class RegistryError(Exception):
    code = "registry_error"
    status = 400

    def __init__(self, message: str = "", detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class VersionNotFound(RegistryError):
    code = "version_not_found"
    status = 404


class ConfigTampered(RegistryError):
    """The folder no longer hashes to what was published: a version folder is never edited."""

    code = "config_tampered"
    status = 422


class SignoffRequired(RegistryError):
    """CTR-19: a release needs a recorded human sign-off."""

    code = "signoff_required"
    status = 403


class SelfSignoff(RegistryError):
    """CTR-19: the person (or run requester) who produced a version cannot be its only sign-off."""

    code = "self_signoff"
    status = 403


class GateFailed(RegistryError):
    """DT-34: a version that failed an evaluation gate is released only with an explicit, noted override."""

    code = "gate_failed"
    status = 403
