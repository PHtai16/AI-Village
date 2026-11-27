"""Minimal Gate E read-only Artifact metadata and download API endpoints."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response

from core.governance.models import TrustedAuthContext
from storage.artifacts import ArtifactRegistryEntry, ArtifactVaultService, VaultError


def _map_vault_error(exc: VaultError) -> HTTPException:
    """Translate domain VaultError codes into explicit HTTP status responses."""
    if exc.error_code in {"PERMISSION_DENIED", "ACCESS_DENIED"}:
        return HTTPException(status_code=403, detail={"error_code": exc.error_code, "message": exc.message})
    if exc.error_code == "ARTIFACT_NOT_FOUND":
        return HTTPException(status_code=404, detail={"error_code": exc.error_code, "message": exc.message})
    if exc.error_code == "ARTIFACT_UNAVAILABLE":
        return HTTPException(status_code=410, detail={"error_code": exc.error_code, "message": exc.message})
    return HTTPException(status_code=500, detail={"error_code": exc.error_code, "message": exc.message})


def create_artifact_api_router(
    vault: ArtifactVaultService,
    *,
    auth_resolver: Callable[[Request], TrustedAuthContext],
) -> APIRouter:
    """Build Gate E FastAPI APIRouter exposing only read-only artifact metadata and download."""
    router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])

    def _resolve_auth(request: Request) -> TrustedAuthContext:
        try:
            ctx = auth_resolver(request)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail={"error_code": "UNAUTHORIZED", "message": str(exc)},
            ) from exc
        if not ctx.authenticated:
            raise HTTPException(
                status_code=401,
                detail={"error_code": "UNAUTHENTICATED", "message": "Authentication required."},
            )
        return ctx

    @router.get("/{artifact_id}")
    def get_artifact_metadata(artifact_id: str, request: Request) -> ArtifactRegistryEntry:
        """Return verified ArtifactRegistryEntry metadata after permission and scope checks."""
        auth = _resolve_auth(request)
        try:
            return vault.get_artifact(
                artifact_id,
                auth_context=auth,
                timestamp=datetime.now(timezone.utc),
            )
        except VaultError as exc:
            raise _map_vault_error(exc) from exc

    @router.get("/{artifact_id}/download")
    def download_artifact(artifact_id: str, request: Request) -> Response:
        """Stream SHA-256 verified ACTIVE artifact bytes with explicit integrity headers."""
        auth = _resolve_auth(request)
        try:
            entry, raw_bytes = vault.read_verified_bytes(
                artifact_id,
                auth_context=auth,
                timestamp=datetime.now(timezone.utc),
            )
        except VaultError as exc:
            raise _map_vault_error(exc) from exc

        safe_filename = Path(entry.relative_path).name.replace('"', "") or f"{entry.artifact_id}.bin"
        return Response(
            content=raw_bytes,
            media_type=entry.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{safe_filename}"',
                "X-Artifact-ID": entry.artifact_id,
                "X-Artifact-Version": entry.version,
                "X-Artifact-SHA256": entry.blob_sha256,
                "Cache-Control": "no-store",
            },
        )

    return router


def create_gate_e_artifact_app(
    vault: ArtifactVaultService,
    *,
    auth_resolver: Callable[[Request], TrustedAuthContext],
) -> FastAPI:
    """Create minimal FastAPI app mounting only Gate E read-only Artifact endpoints."""
    app = FastAPI(title="AI Village Gate E Artifact API", version="0.1.0")
    app.include_router(create_artifact_api_router(vault, auth_resolver=auth_resolver))
    return app
