"""CLI handlers: build → verify → run → report → compare, with a fake service."""

import json
from datetime import date

import pytest

from harbor.eval.cli import (
    build_command,
    compare_command,
    format_gate,
    format_summary,
    main,
    report_command,
    run_command,
    verify_command,
)
from harbor.eval.golden import GoldenSetError, load_golden
from harbor.eval.schema import EvalSeed
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import (
    SearchDiagnostics,
    SearchHit,
    SearchResult,
)


def product(product_id, canonical, title):
    return Product(
        product_id=product_id,
        canonical_product_id=canonical,
        external_product_id=product_id,
        title=title,
        brand="Acme",
        category="户外运动",
        tax_category="general",
        description="",
        highlights=[],
        material_tags=["金属"],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="shop",
        dimensions_cm={"length": 1, "width": 1, "height": 1},
        weight_kg=0.5,
        rating_summary={"score": 4.5, "count": 10},
        skus=[
            Sku(
                sku_id=f"{product_id}-s",
                spec="std",
                price=Money.from_major_units("100", "CNY"),
                stock=5,
            )
        ],
        updated_at=date(2026, 1, 1),
    )


P1 = product("p1", "C1", "Acme Tent")
P3 = product("p3", "C3", "Acme Chair")
CATALOG = [P1, P3]
SEED = EvalSeed(
    seed_id="s1",
    bucket="semantic_vague",
    category="户外运动",
    concept_terms=["Tent"],
    queries=["q"],
)


class FakeService:
    def __init__(self, item):
        self.item = item
        self.calls = 0

    def search(self, query, spec, limit=10):
        self.calls += 1
        return SearchResult(
            hits=[SearchHit(product=self.item, score=1.0, selected_sku=self.item.skus[0])],
            total=1,
            recall_strategy="embedding",
            diagnostics=SearchDiagnostics(latency_ms=7.0),
        )


def build(tmp_path):
    path = tmp_path / "golden.json"
    build_command(
        CATALOG,
        [SEED],
        out=path,
        catalog_sha256="cat",
        bucket_quotas={},
    )
    return path


def test_build_command_writes_a_verifiable_artifact(tmp_path):
    path = build(tmp_path)
    golden = verify_command(path)
    assert golden.manifest.case_count == 1
    assert golden.cases[0].relevant_canonicals == ["C1"]


def test_verify_command_rejects_a_tampered_file(tmp_path):
    path = build(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["query"] = "tampered"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GoldenSetError):
        verify_command(path)


def test_run_command_writes_a_report_and_report_command_summarizes_it(tmp_path):
    golden_path = build(tmp_path)
    run_path = tmp_path / "run.json"
    run_command(golden_path, FakeService(P1), out=run_path)

    report = json.loads(run_path.read_text(encoding="utf-8"))
    assert report["case_results"][0]["ndcg_at_5"] == pytest.approx(1.0)
    assert report["case_results"][0]["latency_ms"] == pytest.approx(7.0)

    summary = report_command(run_path)
    assert summary.overall.ndcg_at_5 == pytest.approx(1.0)
    assert "NDCG@5" in format_summary(summary)


def test_compare_command_reads_two_run_files_and_fails_a_real_drop(tmp_path):
    golden_path = build(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    run_command(golden_path, FakeService(P1), out=baseline_path)
    run_command(golden_path, FakeService(P3), out=candidate_path)

    gate = compare_command(baseline_path, candidate_path)
    assert gate.comparable is True
    assert gate.passed is False
    assert any("ndcg_at_5" in failure for failure in gate.failures)
    assert "passed=False" in format_gate(gate)


def test_main_verifies_and_returns_nonzero_when_the_gate_fails(tmp_path, capsys):
    golden_path = build(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    run_command(golden_path, FakeService(P1), out=baseline_path)
    run_command(golden_path, FakeService(P3), out=candidate_path)

    assert main(["verify", "--golden", str(golden_path)]) == 0
    assert "ok" in capsys.readouterr().out

    code = main(
        [
            "compare",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
        ]
    )
    assert code == 1
    assert "passed=False" in capsys.readouterr().out


def test_loaded_golden_round_trips_through_the_cli(tmp_path):
    path = build(tmp_path)
    assert load_golden(path).manifest.case_count == 1
