"""Google Gemini Model API adapter implementing the Universal Engine Contract."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Callable

import requests

from engines.antigravity.diagnostics import redact_secrets
from engines.connections import CredentialResolver, EngineConnectionRef, EnvCredentialResolver
from engines.contract import (
    EngineAdapter,
    EngineCapabilities,
    EngineError,
    EngineErrorCode,
    EngineErrorSource,
    EngineEvent,
    EnginePlatformKind,
    EngineRequest,
    EngineResult,
    EngineTelemetry,
    EngineToolCallProposal,
)

logger = logging.getLogger(__name__)

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
_RETRY_AFTER_RE = re.compile(
    r"(?i)(?:retry[-_\s]?after[:=\s]+|try again in\s+)(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?\b"
)
HttpPostCallable = Callable[..., Any]


def _scrub_secret(text: str | None, secret: str | None) -> str:
    """Remove an explicit resolved API key and common secret patterns from text."""
    if not text:
        return ""
    cleaned = text.replace(secret, "[REDACTED]") if secret else text
    return redact_secrets(cleaned)


class GoogleGeminiAdapter(EngineAdapter):
    """REST Model API adapter for Google Gemini supporting text, JSON, and tool proposals."""

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver | None = None,
        http_post: HttpPostCallable | None = None,
        default_base_url: str = DEFAULT_BASE_URL,
        default_model_id: str | None = None,
        default_credential_ref: str | None = None,
    ) -> None:
        """Initialize the Google Gemini REST adapter with explicit dependency injection.

        Args:
            credential_resolver: Backend resolver converting ``credential_ref`` to key.
            http_post: Callable matching ``requests.post`` signature for HTTP POST calls.
            default_base_url: Base endpoint URL when ``connection.endpoint_url`` is omitted.
            default_model_id: Optional default Gemini model name when omitted on request.
            default_credential_ref: Optional fallback credential reference when no
                ``connection`` record is attached.
        """
        self._credential_resolver: CredentialResolver = (
            credential_resolver or EnvCredentialResolver()
        )
        self._http_post: HttpPostCallable = http_post or requests.post
        self._default_base_url = default_base_url.rstrip("/")
        self._default_model_id = default_model_id
        self._default_credential_ref = default_credential_ref

    @property
    def provider_id(self) -> str:
        """Return ``google_gemini``."""
        return "google_gemini"

    @property
    def adapter_id(self) -> str:
        """Return ``gemini_api``."""
        return "gemini_api"

    @property
    def platform_kind(self) -> EnginePlatformKind:
        """Return ``model_api``."""
        return "model_api"

    def get_capabilities(
        self,
        connection: EngineConnectionRef | None = None,
    ) -> EngineCapabilities:
        """Declare verified Model API capabilities with zero local tool execution claims."""
        _ = connection
        return EngineCapabilities(
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            platform_kind=self.platform_kind,
            text_generation="SUPPORTED",
            structured_output="SUPPORTED",
            streaming="NOT_VERIFIED",
            tool_calling_proposal="SUPPORTED",
            native_tool_execution="UNSUPPORTED",
            workspace_access="UNSUPPORTED",
            session_continuation="UNSUPPORTED",
            cancellation="UNSUPPORTED",
            usage_telemetry="SUPPORTED",
            model_discovery="NOT_VERIFIED",
            requires_authentication=True,
            data_residency="cloud",
            evidence_notes={
                "protocol": "Gemini REST v1beta generateContent",
                "native_tool_boundary": "Model API emits proposals only; Tool Runtime required for host execution.",
            },
        )

    @staticmethod
    def _normalize_model_id(raw_model_id: str | None) -> str | None:
        """Validate and normalize Gemini model identifiers safely for URL composition."""
        if raw_model_id is None:
            return None
        cleaned = raw_model_id.strip().removeprefix("models/")
        if not cleaned or ".." in cleaned or not _MODEL_ID_RE.fullmatch(cleaned):
            return None
        return cleaned

    @staticmethod
    def _build_request_body(request: EngineRequest) -> dict[str, Any]:
        """Serialize a provider-neutral ``EngineRequest`` to Gemini ``generateContent`` JSON."""
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": request.prompt}],
                }
            ]
        }
        if request.context and request.context.strip():
            body["systemInstruction"] = {"parts": [{"text": request.context.strip()}]}

        if request.execution_mode == "structured" or request.response_schema is not None:
            gen_config: dict[str, Any] = {"responseMimeType": "application/json"}
            if request.response_schema is not None:
                gen_config["responseSchema"] = request.response_schema
            body["generationConfig"] = gen_config

        if request.tool_declarations:
            body["tools"] = [{"functionDeclarations": request.tool_declarations}]

        return body

    @staticmethod
    def _classify_http_error(
        *,
        status_code: int,
        headers: dict[str, Any] | Any,
        raw_body: str,
        secret: str | None,
        provider_id: str,
        connection_id: str | None,
    ) -> EngineError:
        """Normalize HTTP error responses from Gemini REST API without leaking secrets."""
        scrubbed_body = _scrub_secret(raw_body, secret)
        message_text = scrubbed_body
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                err_obj = parsed["error"]
                raw_msg = str(err_obj.get("message") or scrubbed_body)
                message_text = _scrub_secret(raw_msg, secret)
        except json.JSONDecodeError:
            parsed = None

        lowered = message_text.lower()
        retry_after_seconds: float | None = None
        code: EngineErrorCode
        error_source: EngineErrorSource = "provider"
        retryable = False

        if status_code in (401, 403) or (
            status_code == 400
            and any(tok in lowered for tok in ("api key not valid", "api_key_invalid", "permission_denied"))
        ):
            code = "AUTH_ERROR"
        elif status_code == 429:
            if any(
                tok in lowered
                for tok in (
                    "exceeded your current quota",
                    "quota exceeded",
                    "quota",
                    "billing",
                )
            ):
                code = "QUOTA_EXCEEDED"
                retryable = False
            else:
                code = "RATE_LIMITED"
                retryable = True
                retry_hdr = headers.get("Retry-After") if hasattr(headers, "get") else None
                if retry_hdr is not None:
                    try:
                        parsed_hdr = float(str(retry_hdr).strip())
                        if parsed_hdr >= 0:
                            retry_after_seconds = parsed_hdr
                    except ValueError:
                        retry_after_seconds = None
                if retry_after_seconds is None:
                    match = _RETRY_AFTER_RE.search(message_text)
                    if match:
                        try:
                            parsed_delay = float(match.group(1))
                            if parsed_delay >= 0:
                                retry_after_seconds = parsed_delay
                        except ValueError:
                            retry_after_seconds = None
        elif status_code == 404 or (
            status_code == 400
            and any(tok in lowered for tok in ("model", "not found for api version"))
        ):
            code = "INVALID_MODEL"
        else:
            code = "PROVIDER_ERROR"
            retryable = 500 <= status_code < 600

        summary = (
            message_text.splitlines()[0][:500]
            if message_text.strip()
            else f"Gemini API returned HTTP {status_code}."
        )
        return EngineError(
            error_code=code,
            error_source=error_source,
            message=summary,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
            provider_id=provider_id,
            connection_id=connection_id,
            http_status=status_code,
            redacted_details=scrubbed_body[:1500],
        )

    @staticmethod
    def _parse_usage_telemetry(payload: dict[str, Any], duration_ms: int) -> EngineTelemetry:
        """Build ``EngineTelemetry`` strictly from integer fields in ``usageMetadata``."""
        raw_usage = payload.get("usageMetadata")
        if not isinstance(raw_usage, dict):
            return EngineTelemetry(
                duration_ms=duration_ms,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            )

        def _opt_int(key: str) -> int | None:
            val = raw_usage.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                return val
            return None

        input_tokens = _opt_int("promptTokenCount")
        output_tokens = _opt_int("candidatesTokenCount")
        total_tokens = _opt_int("totalTokenCount")
        thinking_tokens = _opt_int("thoughtsTokenCount")
        cached_tokens = _opt_int("cachedContentTokenCount")

        measured = any(
            metric is not None
            for metric in (
                input_tokens,
                output_tokens,
                total_tokens,
                thinking_tokens,
                cached_tokens,
            )
        )
        if not measured:
            return EngineTelemetry(
                duration_ms=duration_ms,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            )

        safe_raw = {
            key: raw_usage[key]
            for key in (
                "promptTokenCount",
                "candidatesTokenCount",
                "totalTokenCount",
                "thoughtsTokenCount",
                "cachedContentTokenCount",
            )
            if isinstance(raw_usage.get(key), int) and not isinstance(raw_usage.get(key), bool)
        }
        return EngineTelemetry(
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            thinking_tokens=thinking_tokens,
            cached_tokens=cached_tokens,
            provider_reported_usage=safe_raw,
            estimated_cost_usd=None,
            actual_cost_usd=None,
            telemetry_status="VERIFIED_MEASURED",
            telemetry_source="provider_api",
        )

    @staticmethod
    def _extract_candidates(
        payload: dict[str, Any],
    ) -> tuple[str | None, list[EngineToolCallProposal]]:
        """Extract text parts and non-executed tool call proposals from Gemini response JSON."""
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("Gemini response JSON did not contain non-empty 'candidates'.")

        first = candidates[0]
        if not isinstance(first, dict):
            raise ValueError("Gemini response candidate entry is not a JSON object.")

        content = first.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            finish_reason = first.get("finishReason")
            raise ValueError(
                f"Gemini candidate omitted content parts (finishReason={finish_reason!r})."
            )

        text_chunks: list[str] = []
        proposals: list[EngineToolCallProposal] = []
        for idx, part in enumerate(parts, start=1):
            if not isinstance(part, dict):
                continue
            text_val = part.get("text")
            if isinstance(text_val, str) and text_val:
                text_chunks.append(text_val)
            func_call = part.get("functionCall")
            if isinstance(func_call, dict):
                name = str(func_call.get("name") or "").strip()
                raw_args = func_call.get("args")
                args_dict = raw_args if isinstance(raw_args, dict) else {}
                if name:
                    proposals.append(
                        EngineToolCallProposal(
                            proposal_id=f"proposal-{idx}",
                            tool_name=name,
                            arguments=args_dict,
                            executed=False,
                            source="model_api_proposal",
                        )
                    )

        response_text = "".join(text_chunks) if text_chunks else None
        return response_text, proposals

    def execute(
        self,
        request: EngineRequest,
        *,
        connection: EngineConnectionRef | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        """Send one validated non-streaming request to Gemini REST API via backend headers."""
        connection_id = connection.connection_id if connection else request.connection_id
        if connection is not None and not connection.enabled:
            return self.build_blocked_result(
                request,
                EngineError(
                    error_code="CONNECTION_DISABLED",
                    error_source="credential",
                    message=f"Connection '{connection.connection_id}' is disabled.",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection.connection_id,
                ),
                connection_id=connection_id,
            )

        capabilities = self.get_capabilities(connection)
        cap_error = self.validate_request_capabilities(
            request,
            capabilities,
            connection_id=connection_id,
        )
        if cap_error is not None:
            return self.build_blocked_result(
                request,
                cap_error,
                capabilities=capabilities,
                connection_id=connection_id,
            )

        raw_model = request.model_id or (connection.default_model_id if connection else None) or self._default_model_id
        model_id = self._normalize_model_id(raw_model)
        if model_id is None:
            now = datetime.now(timezone.utc)
            return EngineResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                connection_id=connection_id,
                model_id=raw_model,
                platform_kind=self.platform_kind,
                status="FAILED",
                started_at=now,
                finished_at=now,
                duration_ms=0,
                telemetry=EngineTelemetry(
                    duration_ms=0,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                error=EngineError(
                    error_code="INVALID_MODEL",
                    error_source="validation",
                    message=(
                        f"Missing or invalid Gemini model_id: {raw_model!r}."
                        if raw_model is not None
                        else "Gemini API requires an explicit model_id on request or connection."
                    ),
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                ),
                capability_evidence=capabilities.to_evidence_map(),
            )

        credential_ref = (
            connection.credential_ref if connection and connection.credential_ref else self._default_credential_ref
        )
        secret = (
            self._credential_resolver.resolve_secret(credential_ref)
            if credential_ref
            else None
        )
        if secret is None:
            return self.build_blocked_result(
                request,
                EngineError(
                    error_code="CREDENTIAL_UNAVAILABLE",
                    error_source="credential",
                    message=(
                        f"Credential reference '{credential_ref}' is not configured in backend environment."
                        if credential_ref
                        else "No credential_ref configured for Google Gemini request."
                    ),
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                ),
                capabilities=capabilities,
                connection_id=connection_id,
                model_id=model_id,
            )

        base_url = (
            connection.endpoint_url
            if connection and connection.endpoint_url
            else self._default_base_url
        )
        endpoint = f"{base_url}/v1beta/models/{model_id}:generateContent"
        body = self._build_request_body(request)
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": secret,
        }

        events: list[EngineEvent] = []

        def _record_event(ev: EngineEvent) -> None:
            events.append(ev)
            if on_event is not None:
                on_event(ev)

        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        _record_event(
            EngineEvent(
                event_id=f"{request.request_id}-ev-1",
                sequence_number=1,
                timestamp=started_at,
                event_type="execution_started",
                source="adapter",
                provider_id=self.provider_id,
                connection_id=connection_id,
                status="STARTED",
                redacted_payload={
                    "endpoint": endpoint,
                    "model_id": model_id,
                    "credential_status": "resolved_in_memory",
                },
            )
        )

        logger.info(
            "Calling Google Gemini endpoint=%s model=%s connection=%s",
            endpoint,
            model_id,
            connection_id,
        )

        try:
            response = self._http_post(
                endpoint,
                headers=headers,
                json=body,
                timeout=request.timeout_seconds,
            )
        except (requests.Timeout, TimeoutError) as exc:
            finished_at = datetime.now(timezone.utc)
            duration_ms = max(0, int((time.monotonic() - t0) * 1000))
            msg = _scrub_secret(str(exc), secret) or f"Gemini request timed out after {request.timeout_seconds}s."
            err = EngineError(
                error_code="TIMED_OUT",
                error_source="network",
                message=msg[:500],
                retryable=False,
                provider_id=self.provider_id,
                connection_id=connection_id,
            )
            _record_event(
                EngineEvent(
                    event_id=f"{request.request_id}-ev-{len(events) + 1}",
                    sequence_number=len(events) + 1,
                    timestamp=finished_at,
                    event_type="process_timed_out",
                    source="adapter",
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                    status="TIMED_OUT",
                    redacted_payload={"message": err.message},
                )
            )
            return EngineResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                connection_id=connection_id,
                model_id=model_id,
                platform_kind=self.platform_kind,
                status="TIMED_OUT",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=EngineTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                events=events,
                error=err,
                capability_evidence=capabilities.to_evidence_map(),
            )
        except (requests.RequestException, OSError) as exc:
            finished_at = datetime.now(timezone.utc)
            duration_ms = max(0, int((time.monotonic() - t0) * 1000))
            msg = _scrub_secret(str(exc), secret) or "Network error communicating with Gemini API."
            err = EngineError(
                error_code="NETWORK_ERROR",
                error_source="network",
                message=msg[:500],
                retryable=True,
                provider_id=self.provider_id,
                connection_id=connection_id,
            )
            _record_event(
                EngineEvent(
                    event_id=f"{request.request_id}-ev-{len(events) + 1}",
                    sequence_number=len(events) + 1,
                    timestamp=finished_at,
                    event_type="provider_error",
                    source="adapter",
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                    status="FAILED",
                    redacted_payload={"error_code": err.error_code, "message": err.message},
                )
            )
            return EngineResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                connection_id=connection_id,
                model_id=model_id,
                platform_kind=self.platform_kind,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=EngineTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                events=events,
                error=err,
                capability_evidence=capabilities.to_evidence_map(),
            )

        finished_at = datetime.now(timezone.utc)
        duration_ms = max(0, int((time.monotonic() - t0) * 1000))
        status_code = int(getattr(response, "status_code", 0))
        raw_text = str(getattr(response, "text", "") or "")

        if status_code < 200 or status_code >= 300:
            err = self._classify_http_error(
                status_code=status_code,
                headers=getattr(response, "headers", {}),
                raw_body=raw_text,
                secret=secret,
                provider_id=self.provider_id,
                connection_id=connection_id,
            )
            _record_event(
                EngineEvent(
                    event_id=f"{request.request_id}-ev-{len(events) + 1}",
                    sequence_number=len(events) + 1,
                    timestamp=finished_at,
                    event_type="provider_error",
                    source="provider",
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                    status="FAILED",
                    redacted_payload={
                        "error_code": err.error_code,
                        "http_status": status_code,
                        "message": err.message,
                    },
                )
            )
            return EngineResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                connection_id=connection_id,
                model_id=model_id,
                platform_kind=self.platform_kind,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=EngineTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                events=events,
                error=err,
                capability_evidence=capabilities.to_evidence_map(),
            )

        try:
            payload = response.json() if hasattr(response, "json") else json.loads(raw_text)
        except (ValueError, json.JSONDecodeError) as exc:
            err = EngineError(
                error_code="INVALID_RESPONSE",
                error_source="provider",
                message=_scrub_secret(f"Gemini returned invalid JSON payload: {exc}", secret),
                retryable=False,
                provider_id=self.provider_id,
                connection_id=connection_id,
                http_status=status_code,
            )
            return EngineResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                connection_id=connection_id,
                model_id=model_id,
                platform_kind=self.platform_kind,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=EngineTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                events=events,
                error=err,
                capability_evidence=capabilities.to_evidence_map(),
            )

        if not isinstance(payload, dict):
            err = EngineError(
                error_code="INVALID_RESPONSE",
                error_source="provider",
                message="Gemini response JSON top-level payload must be an object.",
                retryable=False,
                provider_id=self.provider_id,
                connection_id=connection_id,
                http_status=status_code,
            )
            return EngineResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                connection_id=connection_id,
                model_id=model_id,
                platform_kind=self.platform_kind,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=EngineTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                events=events,
                error=err,
                capability_evidence=capabilities.to_evidence_map(),
            )

        telemetry = self._parse_usage_telemetry(payload, duration_ms)
        try:
            response_text, proposals = self._extract_candidates(payload)
        except ValueError as exc:
            err = EngineError(
                error_code="INVALID_RESPONSE",
                error_source="provider",
                message=_scrub_secret(str(exc), secret),
                retryable=False,
                provider_id=self.provider_id,
                connection_id=connection_id,
                http_status=status_code,
            )
            return EngineResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                adapter_id=self.adapter_id,
                connection_id=connection_id,
                model_id=model_id,
                platform_kind=self.platform_kind,
                status="FAILED",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=telemetry,
                events=events,
                error=err,
                capability_evidence=capabilities.to_evidence_map(),
            )

        structured_output: dict[str, Any] | list[Any] | None = None
        if request.execution_mode == "structured" or request.response_schema is not None:
            if not response_text:
                err = EngineError(
                    error_code="INVALID_RESPONSE",
                    error_source="provider",
                    message="Structured output mode requested, but Gemini returned no text.",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                    http_status=status_code,
                )
                return EngineResult(
                    request_id=request.request_id,
                    provider_id=self.provider_id,
                    adapter_id=self.adapter_id,
                    connection_id=connection_id,
                    model_id=model_id,
                    platform_kind=self.platform_kind,
                    status="FAILED",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    telemetry=telemetry,
                    events=events,
                    error=err,
                    capability_evidence=capabilities.to_evidence_map(),
                )
            try:
                parsed_structured = json.loads(response_text)
            except json.JSONDecodeError as exc:
                err = EngineError(
                    error_code="INVALID_RESPONSE",
                    error_source="provider",
                    message=f"Gemini structured output was not valid JSON: {exc}",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                    http_status=status_code,
                )
                return EngineResult(
                    request_id=request.request_id,
                    provider_id=self.provider_id,
                    adapter_id=self.adapter_id,
                    connection_id=connection_id,
                    model_id=model_id,
                    platform_kind=self.platform_kind,
                    status="FAILED",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    telemetry=telemetry,
                    events=events,
                    error=err,
                    capability_evidence=capabilities.to_evidence_map(),
                )
            if not isinstance(parsed_structured, (dict, list)):
                err = EngineError(
                    error_code="INVALID_RESPONSE",
                    error_source="provider",
                    message="Structured output JSON must deserialize to an object or array.",
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                    http_status=status_code,
                )
                return EngineResult(
                    request_id=request.request_id,
                    provider_id=self.provider_id,
                    adapter_id=self.adapter_id,
                    connection_id=connection_id,
                    model_id=model_id,
                    platform_kind=self.platform_kind,
                    status="FAILED",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    telemetry=telemetry,
                    events=events,
                    error=err,
                    capability_evidence=capabilities.to_evidence_map(),
                )
            structured_output = parsed_structured

        for proposal in proposals:
            _record_event(
                EngineEvent(
                    event_id=f"{request.request_id}-ev-{len(events) + 1}",
                    sequence_number=len(events) + 1,
                    timestamp=finished_at,
                    event_type="tool_call_proposed",
                    source="provider",
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                    status="COMPLETED",
                    tool_name=proposal.tool_name,
                    redacted_payload={
                        "proposal_id": proposal.proposal_id,
                        "argument_keys": sorted(proposal.arguments.keys()),
                        "executed": False,
                    },
                )
            )

        _record_event(
            EngineEvent(
                event_id=f"{request.request_id}-ev-{len(events) + 1}",
                sequence_number=len(events) + 1,
                timestamp=finished_at,
                event_type="execution_completed",
                source="provider",
                provider_id=self.provider_id,
                connection_id=connection_id,
                status="COMPLETED",
                redacted_payload={"model_id": model_id},
                telemetry=telemetry,
            )
        )

        reported_model = str(payload.get("modelVersion") or model_id)
        return EngineResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            connection_id=connection_id,
            model_id=reported_model,
            platform_kind=self.platform_kind,
            status="SUCCEEDED",
            response_text=response_text,
            structured_output=structured_output,
            tool_call_proposals=proposals,
            observed_tool_names=[],
            session_reference=request.session_reference,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            telemetry=telemetry,
            events=events,
            error=None,
            capability_evidence=capabilities.to_evidence_map(),
        )
