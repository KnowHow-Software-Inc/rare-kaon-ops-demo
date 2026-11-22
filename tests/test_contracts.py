"""Contract tests for the frozen canonical ReportModel and privacy/approval guardrails."""

from __future__ import annotations

from engine.contracts import error_envelope, ok_envelope
from engine.model import (
    BridgeDriver,
    CheckItem,
    KpiField,
    PendingChange,
    ReportMeta,
    ReportModel,
    SourceRef,
    VarianceBridge,
)


def _sample_model() -> ReportModel:
    return ReportModel(
        meta=ReportMeta(
            company_name="Heartland Mechanical Services, LLC",
            module="weekly",
            period="2026-11-20",
            period_label="Week ended 20 November 2026",
            version="0.1.0",
            draft_number=1,
            status="draft",
            created_at="2026-11-20T14:00:00Z",
            approver="Claire Vance",
        ),
        kpis=[
            KpiField(key="opening_cash", label="Opening Cash", value=1840000.00, unit="USD"),
        ],
        variance_bridge=VarianceBridge(
            baseline_period="2026-11-13",
            baseline_label="Issued Edition 1 (13 Nov 2026)",
            shared_date="2026-11-20",
            prior_value=1805850.25,
            current_value=1840000.00,
            delta=34149.75,
            drivers=[
                BridgeDriver(
                    key="early_ar",
                    label="Early collection — Methodist Health Facilities",
                    amount=64250.00,
                    source_file="frost-operating-2026-11-20.csv",
                    source_date="2026-11-18",
                    source_ref="INV-2026-8814",
                )
            ],
        ),
        checks=[
            CheckItem(
                id="WK-REV-001",
                group="Weekly Cash",
                title="Estimated fleet fuel card auto-draft",
                severity="open_review",
                detail="Placeholder $14,200 pending monthly statement.",
                owner="Claire Vance",
            )
        ],
        sources=[
            SourceRef(
                feed_id="frost_operating",
                file_name="frost-operating-2026-11-20.csv",
                as_of="2026-11-20",
                sensitivity="internal",
                attachable=True,
            ),
            SourceRef(
                feed_id="payroll_voucher",
                file_name="payroll-voucher-2026-11-15.csv",
                as_of="2026-11-15",
                sensitivity="restricted",
                attachable=True,  # Must be forced to False programmatically!
            ),
        ],
    )


def test_restricted_source_cannot_be_attachable() -> None:
    model = _sample_model()
    restricted = [s for s in model.sources if s.feed_id == "payroll_voucher"][0]
    assert restricted.attachable is False
    assert [s.feed_id for s in model.attachable_sources()] == ["frost_operating"]
    assert [s.feed_id for s in model.non_attachable_sources()] == ["payroll_voucher"]


def test_open_review_does_not_block_approval_but_blocking_and_pending_do() -> None:
    model = _sample_model()
    assert model.can_approve is True
    assert model.open_review_checks[0].blocks_approval is False

    model.checks.append(
        CheckItem(
            id="PAY-MAP-001",
            group="Payroll",
            title="Unmapped employee department",
            severity="blocking",
            detail="EMP-1048 lacks mapping for TUL-INSTALL-NEW.",
        )
    )
    assert model.blocking_checks[0].blocks_approval is True
    assert model.can_approve is False

    model.checks.pop()
    assert model.can_approve is True

    model.pending_changes.append(
        PendingChange(
            id="CHG-001",
            period="2026-11-20",
            module="weekly",
            kind="payment_deferral",
            summary="Defer LSS-2026-4189 ($25,000) from Week 1 to Week 2.",
        )
    )
    assert model.can_approve is False


def test_report_model_round_trip() -> None:
    model = _sample_model()
    raw = model.to_dict()
    assert raw["summary_counts"]["can_approve"] is True
    assert raw["summary_counts"]["open_review_checks"] == 1
    restored = ReportModel.from_dict(raw)
    assert restored.meta.company_name == model.meta.company_name
    assert restored.kpis[0].value == 1840000.00
    assert restored.variance_bridge is not None
    assert restored.variance_bridge.drivers[0].source_ref == "INV-2026-8814"


def test_cli_envelopes() -> None:
    ok = ok_envelope("overview", {"periods": ["2026-11-20"]}, workspace="/demo")
    assert ok["ok"] is True
    assert ok["workspace"] == "/demo"

    err = error_envelope("approve", "BLOCKING_CHECKS_OPEN", "1 blocking check", "Resolve check first")
    assert err["ok"] is False
    assert err["error_code"] == "BLOCKING_CHECKS_OPEN"
