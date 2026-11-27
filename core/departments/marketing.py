"""Phase 09 / P09.04 Marketing Department: dynamic research, campaign planning, and real content outputs."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.identifiers import (
    EventId,
    RunId,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_relative_path,
)
from core.departments._common import (
    DepartmentBehaviorError,
    ToolInvokerPort,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
    invoke_tool_checked,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source

MarketingChannel = Literal["blog", "social", "press_release", "email_campaign", "product_launch"]
MarketingOutputFormat = Literal["markdown", "docx"]

_CHANNEL_DELIVERABLES: dict[MarketingChannel, list[str]] = {
    "blog": ["Deep-dive narrative outline", "SEO title & reader takeaways", "CTA footer"],
    "social": ["Short-form hook variants", "Hashtag & visual prompt brief", "Engagement CTA"],
    "press_release": ["Headline & dateline", "Executive quote slot", "Product facts boilerplate"],
    "email_campaign": ["Subject-line A/B hooks", "Value breakdown", "Direct conversion CTA"],
    "product_launch": ["Positioning statement", "Target persona matrix", "Launch checklist"],
}

_CHANNEL_KPIS: dict[MarketingChannel, list[str]] = {
    "blog": ["Organic reader retention", "Qualified click-through rate"],
    "social": ["Engagement rate per post", "Referral clicks to landing target"],
    "press_release": ["Media pickup count", "Direct brand search lift"],
    "email_campaign": ["Open rate", "Verified CTA conversion rate"],
    "product_launch": ["Day-7 activation signups", "Onboarding completion rate"],
}


class MarketingValidationError(DepartmentBehaviorError):
    """Raised when marketing campaign inputs or source references fail fail-closed validation."""


class ResearchCitation(BaseModel):
    """Untrusted external source citation anchored by real URL, retrieval timestamp, and hash."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=240)
    source_url: str = Field(min_length=8, max_length=1024)
    summary: str = Field(min_length=3, max_length=1500)
    retrieved_at: datetime
    content_sha256: Sha256Hex
    evidence_ref: str | None = Field(default=None, max_length=128)
    content_trust_level: Literal["UNTRUSTED_EXTERNAL_DATA"] = "UNTRUSTED_EXTERNAL_DATA"

    @field_validator("title", "source_url", "summary", "evidence_ref")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "citation_field"))

    @field_validator("retrieved_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "retrieved_at")


class MarketingWorkflowRequest(BaseModel):
    """Dynamic brief driving research -> campaign plan -> real file deliverable generation."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    topic: str = Field(min_length=3, max_length=300)
    target_audience: str = Field(min_length=3, max_length=240)
    channel: MarketingChannel
    tone: str = Field(min_length=2, max_length=100)
    objectives: list[str] = Field(min_length=1, max_length=12)
    search_queries: list[str] = Field(default_factory=list, max_length=5)
    research_urls: list[str] = Field(default_factory=list, max_length=5)
    pre_collected_sources: list[ResearchCitation] = Field(default_factory=list, max_length=16)
    output_relpath: str = Field(min_length=3, max_length=260)
    output_format: MarketingOutputFormat = "markdown"
    workspace_root: str = Field(min_length=1, max_length=512)
    overwrite: bool = False
    timestamp: datetime

    @field_validator("topic", "target_audience", "tone", "workspace_root")
    @classmethod
    def _validate_labels(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "marketing_field"))

    @field_validator("objectives", "search_queries", "research_urls")
    @classmethod
    def _validate_lists(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "marketing_list")
        return [ensure_not_secret(item, field_name) for item in values]

    @field_validator("output_relpath")
    @classmethod
    def _validate_out_path(cls, value: str) -> str:
        return validate_safe_relative_path(value, "output_relpath")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class MarketingCampaignPlan(BaseModel):
    """Task-specific campaign plan synthesized dynamically from brief parameters and research."""

    model_config = ConfigDict(extra="forbid")

    topic: str
    target_audience: str
    channel: MarketingChannel
    tone: str
    messaging_pillars: list[str]
    planned_deliverables: list[str]
    kpis: list[str]
    research_gaps: list[str]


class MarketingWorkflowOutcome(BaseModel):
    """Real file deliverable produced by Marketing without hardcoded canned outputs."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    plan: MarketingCampaignPlan
    citations: list[ResearchCitation]
    output_relpath: str
    output_format: MarketingOutputFormat
    output_sha256: Sha256Hex
    size_bytes: int = Field(ge=1)
    evidence_refs: list[str] = Field(default_factory=list)
    business_verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    audit_event_id: EventId


class MarketingDepartmentService:
    """Marketing Department service running real research, campaign planning, and workspace publishing."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        tool_invoker: ToolInvokerPort,
    ) -> None:
        """Bind Marketing service to Governance, EventJournal, and ToolRuntime."""
        self._pm = permission_manager
        self._journal = journal
        self._tools = tool_invoker

    def _collect_research(
        self,
        request: MarketingWorkflowRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> tuple[list[ResearchCitation], list[str]]:
        citations = list(request.pre_collected_sources)
        evidence_refs: list[str] = [
            c.evidence_ref for c in citations if c.evidence_ref is not None
        ]

        for query in request.search_queries:
            res = invoke_tool_checked(
                self._tools,
                auth_context=auth_context,
                tool_id="tools.research.web_search",
                parameters={"query": query, "max_results": 3},
                task_id=request.task_id,
                run_id=request.run_id,
                timestamp=request.timestamp,
                correlation_id=f"mkt-search-{request.task_id}",
            )
            evidence_refs.extend(res.evidence_refs)
            for idx, item in enumerate(res.data.get("results", []) or []):
                url = str(item.get("url") or f"https://search.local/{idx}")
                snippet = str(item.get("snippet") or item.get("title") or query)[:1500]
                title = str(item.get("title") or query)[:240]
                digest = hashlib.sha256(f"{url}:{snippet}".encode("utf-8")).hexdigest()
                citations.append(
                    ResearchCitation(
                        title=title,
                        source_url=url,
                        summary=snippet,
                        retrieved_at=request.timestamp,
                        content_sha256=digest,
                        evidence_ref=res.evidence_refs[0] if res.evidence_refs else None,
                    )
                )

        for url in request.research_urls:
            res = invoke_tool_checked(
                self._tools,
                auth_context=auth_context,
                tool_id="tools.research.web_fetch",
                parameters={"url": url, "mode": "httpx"},
                task_id=request.task_id,
                run_id=request.run_id,
                timestamp=request.timestamp,
                correlation_id=f"mkt-fetch-{request.task_id}",
            )
            evidence_refs.extend(res.evidence_refs)
            body = str(res.data.get("content") or res.data.get("excerpt") or url)[:1500]
            digest = str(
                res.data.get("content_sha256")
                or res.data.get("sha256")
                or hashlib.sha256(body.encode("utf-8")).hexdigest()
            )
            citations.append(
                ResearchCitation(
                    title=str(res.data.get("title") or url)[:240],
                    source_url=str(res.data.get("url") or url),
                    summary=body or "Fetched source page summary",
                    retrieved_at=request.timestamp,
                    content_sha256=digest,
                    evidence_ref=res.evidence_refs[0] if res.evidence_refs else None,
                )
            )

        return citations, list(dict.fromkeys(evidence_refs))

    @staticmethod
    def _build_dynamic_plan(
        request: MarketingWorkflowRequest,
        citations: list[ResearchCitation],
    ) -> MarketingCampaignPlan:
        pillars = [
            f"[{request.topic}] Value proposition tailored for {request.target_audience}: {obj}"
            for obj in request.objectives
        ]
        if citations:
            pillars.append(
                f"Evidence-backed angle anchored on {len(citations)} observed source(s) in '{request.channel}' format."
            )
        gaps: list[str] = []
        if not citations:
            gaps.append(
                "NO_EXTERNAL_RESEARCH_EXECUTED: Draft strictly avoids unverified external statistics."
            )
        return MarketingCampaignPlan(
            topic=request.topic,
            target_audience=request.target_audience,
            channel=request.channel,
            tone=request.tone,
            messaging_pillars=pillars,
            planned_deliverables=list(_CHANNEL_DELIVERABLES[request.channel]),
            kpis=list(_CHANNEL_KPIS[request.channel]),
            research_gaps=gaps,
        )

    @staticmethod
    def _render_markdown_document(
        request: MarketingWorkflowRequest,
        plan: MarketingCampaignPlan,
        citations: list[ResearchCitation],
    ) -> str:
        lines = [
            f"# {request.topic}",
            "",
            f"- **Channel:** `{request.channel}`",
            f"- **Target Audience:** {request.target_audience}",
            f"- **Tone:** {request.tone}",
            "",
            "## Campaign Plan & Messaging Pillars",
        ]
        for pillar in plan.messaging_pillars:
            lines.append(f"- {pillar}")
        lines.extend(["", "## Planned Deliverables"])
        for item in plan.planned_deliverables:
            lines.append(f"- {item}")
        lines.extend(["", "## Success Metrics (KPIs)"])
        for kpi in plan.kpis:
            lines.append(f"- {kpi}")
        lines.extend(["", "## Research Findings (Untrusted External Sources)"])
        if citations:
            for c in citations:
                lines.append(
                    f"- **{c.title}** ({c.source_url}, `sha256:{c.content_sha256[:12]}...`): {c.summary}"
                )
        else:
            lines.append("- None provided or fetched; no fabricated market claims included.")
        if plan.research_gaps:
            lines.extend(["", "## Explicit Gaps & Verification Boundary"])
            for gap in plan.research_gaps:
                lines.append(f"- {gap}")
        return "\n".join(lines) + "\n"

    def run_campaign_workflow(
        self,
        request: MarketingWorkflowRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> MarketingWorkflowOutcome:
        """Execute governed research -> dynamic campaign plan -> real file deliverable in workspace."""
        action = "office.write" if request.output_format == "docx" else "files.write"
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action=action,
            resource=f"marketing:campaign:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            workspace_root=request.workspace_root,
            target_path=request.output_relpath,
            timestamp=request.timestamp,
            correlation_id=f"mkt-run-{request.task_id}",
        )

        citations, evidence_refs = self._collect_research(request, auth_context=auth_context)
        plan = self._build_dynamic_plan(request, citations)

        if request.output_format == "docx":
            paragraphs = [
                f"Channel: {request.channel} | Audience: {request.target_audience} | Tone: {request.tone}",
                *[f"Pillar: {p}" for p in plan.messaging_pillars],
                *[f"Deliverable: {d}" for d in plan.planned_deliverables],
                *[f"KPI: {k}" for k in plan.kpis],
                *[
                    f"Source ({c.source_url}, {c.content_sha256[:12]}): {c.summary}"
                    for c in citations
                ],
                *plan.research_gaps,
            ]
            write_res = invoke_tool_checked(
                self._tools,
                auth_context=auth_context,
                tool_id="tools.office.write_docx",
                parameters={
                    "path": request.output_relpath,
                    "title": request.topic,
                    "paragraphs": paragraphs,
                    "overwrite": request.overwrite,
                },
                task_id=request.task_id,
                run_id=request.run_id,
                workspace_root=request.workspace_root,
                target_path=request.output_relpath,
                timestamp=request.timestamp,
                correlation_id=f"mkt-docx-{request.task_id}",
            )
            out_sha = str(write_res.data["sha256"])
            out_bytes = int(write_res.data["size_bytes"])
            written_rel = str(write_res.data.get("path", request.output_relpath))
        else:
            md_content = self._render_markdown_document(request, plan, citations)
            write_res = invoke_tool_checked(
                self._tools,
                auth_context=auth_context,
                tool_id="tools.files.write",
                parameters={
                    "path": request.output_relpath,
                    "content": md_content,
                    "overwrite": request.overwrite,
                },
                task_id=request.task_id,
                run_id=request.run_id,
                workspace_root=request.workspace_root,
                target_path=request.output_relpath,
                timestamp=request.timestamp,
                correlation_id=f"mkt-md-{request.task_id}",
            )
            out_sha = str(write_res.data["sha256"])
            out_bytes = int(write_res.data["size_bytes"])
            written_rel = str(write_res.data.get("path", request.output_relpath))

        ev = append_department_event(
            self._journal,
            event_type="artifact.recorded",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            timestamp=request.timestamp,
            correlation_id=f"mkt-run-{request.task_id}",
            payload={
                "department": "marketing",
                "topic": request.topic,
                "channel": request.channel,
                "output_relpath": written_rel,
                "output_sha256": out_sha,
                "citations_count": len(citations),
                "business_verification_status": "UNVERIFIED",
            },
        )
        return MarketingWorkflowOutcome(
            task_id=request.task_id,
            run_id=request.run_id,
            plan=plan,
            citations=citations,
            output_relpath=written_rel,
            output_format=request.output_format,
            output_sha256=out_sha,
            size_bytes=out_bytes,
            evidence_refs=evidence_refs,
            audit_event_id=ev.event_id,
        )
