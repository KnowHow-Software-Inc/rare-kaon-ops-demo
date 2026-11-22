"""Frozen canonical data model (`report-model.json`) shared across all modules and renderers.

Every workflow run writes one serialized ReportModel to `report-model.json`.
PDFs, static HTML dashboards (`Reports.html`, `Setup.html`), Excel workbooks,
and structured CLI JSON queries all read this single model so cross-surface KPI
drift cannot occur.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ModuleName = Literal["weekly", "payroll"]
CheckSeverity = Literal["blocking", "open_review", "note"]
Sensitivity = Literal["board", "internal", "restricted"]
KpiUnit = Literal["USD", "pct", "count", "text"]
ReportStatus = Literal["draft", "approved"]
ComparisonKind = Literal["prior", "plan", "limit", "label"]


@dataclass
class KpiField:
    """Single typed KPI rendered identically across JSON, HTML, and PDF."""

    key: str
    label: str
    value: float | int | str | None
    unit: KpiUnit = "USD"
    status: str = "actual"
    note: str = ""
    comparison_value: float | int | None = None
    comparison_label: str | None = None
    comparison_kind: ComparisonKind = "prior"


@dataclass
class BridgeDriver:
    """One traceable driver in a period-over-period variance bridge."""

    key: str
    label: str
    amount: float
    source_file: str = ""
    source_date: str = ""
    source_ref: str = ""
    note: str = ""


@dataclass
class VarianceBridge:
    """Reconciliation from the prior issued pack to the current draft."""

    baseline_period: str
    baseline_label: str
    shared_date: str
    prior_value: float
    current_value: float
    delta: float
    ending_value: float | None = None
    drivers: list[BridgeDriver] = field(default_factory=list)
    moved_payments: list[dict[str, Any]] = field(default_factory=list)
    source_citations: list[str] = field(default_factory=list)


@dataclass
class CheckItem:
    """Deterministic control check with explicit approval-blocking semantics."""

    id: str
    group: str
    title: str
    severity: CheckSeverity
    detail: str
    owner: str = ""
    next_step: str = ""
    amount: float | None = None
    blocks_approval: bool = False

    def __post_init__(self) -> None:
        # Only `blocking` checks prevent approval; `open_review` and `note` never block.
        self.blocks_approval = self.severity == "blocking"


@dataclass
class PendingChange:
    """Unconfirmed operator decision awaiting explicit confirmation or decline."""

    id: str
    period: str
    module: ModuleName
    kind: str
    summary: str
    source_file: str = ""
    needs_decision: bool = True
    rationale: str = ""
    recorded_by: str = ""
    recorded_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceRef:
    """Retained source citation with programmatic privacy enforcement."""

    feed_id: str
    file_name: str
    as_of: str | None
    sensitivity: Sensitivity
    attachable: bool
    relative_path: str | None = None
    row_refs: list[str] = field(default_factory=list)
    detail: str = ""

    def __post_init__(self) -> None:
        # Programmatic privacy wall: restricted files can never be marked attachable.
        if self.sensitivity == "restricted":
            self.attachable = False


@dataclass
class ReportMeta:
    """Run identity, draft/edition lifecycle, and schema metadata."""

    company_name: str
    module: ModuleName
    period: str
    period_label: str
    version: str
    draft_number: int
    status: ReportStatus
    created_at: str
    approver: str | None = None
    is_approved: bool = False
    edition_number: int | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    schema_version: int = 1


@dataclass
class ReportModel:
    """Canonical single source of truth for a module run."""

    meta: ReportMeta
    kpis: list[KpiField] = field(default_factory=list)
    variance_bridge: VarianceBridge | None = None
    sections: dict[str, Any] = field(default_factory=dict)
    checks: list[CheckItem] = field(default_factory=list)
    pending_changes: list[PendingChange] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    narrative: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def blocking_checks(self) -> list[CheckItem]:
        return [c for c in self.checks if c.severity == "blocking"]

    @property
    def open_review_checks(self) -> list[CheckItem]:
        return [c for c in self.checks if c.severity == "open_review"]

    @property
    def note_checks(self) -> list[CheckItem]:
        return [c for c in self.checks if c.severity == "note"]

    @property
    def can_approve(self) -> bool:
        open_pending = [p for p in self.pending_changes if p.needs_decision]
        return len(self.blocking_checks) == 0 and len(open_pending) == 0

    def kpi_map(self) -> dict[str, KpiField]:
        return {k.key: k for k in self.kpis}

    def attachable_sources(self) -> list[SourceRef]:
        return [s for s in self.sources if s.attachable and s.sensitivity != "restricted"]

    def non_attachable_sources(self) -> list[SourceRef]:
        return [s for s in self.sources if not s.attachable or s.sensitivity == "restricted"]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        open_pending = [p for p in self.pending_changes if p.needs_decision]
        payload["summary_counts"] = {
            "blocking_checks": len(self.blocking_checks),
            "open_review_checks": len(self.open_review_checks),
            "notes": len(self.note_checks),
            "pending_changes": len(open_pending),
            "can_approve": self.can_approve,
        }
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportModel":
        meta = ReportMeta(**data["meta"])
        kpis = [KpiField(**item) for item in data.get("kpis", [])]
        raw_bridge = data.get("variance_bridge")
        bridge = None
        if raw_bridge:
            drivers = [BridgeDriver(**d) for d in raw_bridge.get("drivers", [])]
            bridge = VarianceBridge(**{**raw_bridge, "drivers": drivers})
        checks = [CheckItem(**item) for item in data.get("checks", [])]
        pending = [PendingChange(**item) for item in data.get("pending_changes", [])]
        sources = [SourceRef(**item) for item in data.get("sources", [])]
        return cls(
            meta=meta,
            kpis=kpis,
            variance_bridge=bridge,
            sections=dict(data.get("sections", {})),
            checks=checks,
            pending_changes=pending,
            sources=sources,
            narrative=dict(data.get("narrative", {})),
            artifacts=dict(data.get("artifacts", {})),
        )
