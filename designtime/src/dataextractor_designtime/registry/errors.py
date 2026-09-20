"""Registry errors. Each carries the HTTP status the API surfaces it as."""

from __future__ import annotations


class RegistryError(Exception):
    code = "registry_error"
    status = 400


class VersionExists(RegistryError):
    """CTR-01: a published version is immutable; republishing it is refused."""

    code = "version_exists"
    status = 409


class PackageNotFound(RegistryError):
    code = "package_not_found"
    status = 404


class PackageCorrupt(RegistryError):
    """CTR-02: a checksum recomputed on load did not match the manifest."""

    code = "package_corrupt"
    status = 422


class EngineIncompatible(RegistryError):
    """CTR-03: the engine version falls outside the package's engine_range."""

    code = "engine_incompatible"
    status = 409


class TypeIncomplete(RegistryError):
    """CTR-05: a declared email type is missing its schema or thresholds."""

    code = "type_incomplete"
    status = 422


class VersionBumpInsufficient(RegistryError):
    """CTR-17: the semver bump understates the artifact diff."""

    code = "version_bump_insufficient"
    status = 409


class SignoffRequired(RegistryError):
    """CTR-19: promotion needs a recorded human sign-off."""

    code = "signoff_required"
    status = 403


class GateFailed(RegistryError):
    """DT-34: a package that failed an evaluation gate cannot be promoted."""

    code = "gate_failed"
    status = 403
