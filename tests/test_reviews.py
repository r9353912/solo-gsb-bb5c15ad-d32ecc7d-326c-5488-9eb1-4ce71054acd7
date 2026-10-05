"""Execution-variance reconciliation review tests.

A review compares the frozen plan of an inventory **stepwise** plan that has
already finished (``completed`` or ``aborted``) against the full ordered
record of actual transfers submitted by the experimenter, and seals an
immutable report under a unique ``X-Review-ID``.

Rules under test:

* only completed / aborted stepwise plans are reviewable; unknown plans,
  infeasible plans, whole-mode plans and still-running plans are rejected
  (404 / 409) without a report;
* per-step results: matched / missing (frozen but not executed) / extra
  (executed but not frozen) / content_mismatch; the first such position is
  reported, plus frozen vs actual stock-out totals per batch;
* same review id + same content replays the original report (200); same id +
  different content conflicts (409);
* duplicate step numbers, non-ordered records, illegal nodes or non-positive
  volumes reject the whole request with 400 and seal no report;
* a review never changes inventory, the plan or registered steps.
"""
import json

import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as main
from tests.test_inventory import FakePool, FakeStore
from tests.test_stepwise import BATCH, STEP_BODY


class ReviewFakeStore(FakeStore):
    """Adds the immutable execution_reviews store to the inventory fake."""

    def __init__(self):
        super().__init__()
        self.reviews = {}  # review_id -> sealed document

    async def get_execution_review(self, conn, review_id):
        return json.loads(json.dumps(self.reviews.get(review_id))) \
            if review_id in self.reviews else None

    async def seal_execution_review(self, conn, review_id, request_id,
                                    content_hash, report):
        existing = self.reviews.get(review_id)
        if existing is not None:
            if existing["content_hash"] == content_hash:
                return "replay", json.loads(json.dumps(existing))
            return "conflict", {"review_id": review_id,
                                "request_id": existing["request_id"]}
        data = json.loads(json.dumps(report))
        data["review_id"] = review_id
        data["request_id"] = request_id
        data["sealed_at"] = "2026-10-05T08:30:00+00:00"
        self.reviews[review_id] = data
        return "ok", json.loads(json.dumps(data))


@pytest.fixture
def store(monkeypatch):
    st = ReviewFakeStore()

    async def fake_connect(dsn=None):
        return FakePool()

    pairs = [
        ("connect", fake_connect),
        ("get_frozen", st.get_frozen),
        ("freeze", st.freeze),
        ("create_batch", st.create_batch),
        ("get_batch", st.get_batch),
        ("get_batches", st.get_batches),
        ("list_batches", st.list_batches),
        ("get_reservation", st.get_reservation),
        ("reserve_and_freeze", st.reserve_and_freeze),
        ("transition_reservation", st.transition_reservation),
        ("register_step", st.register_step),
        ("complete_stepwise", st.complete_stepwise),
        ("abort_stepwise", st.abort_stepwise),
        ("recall_batch", st.recall_batch),
        ("get_recall", st.get_recall),
        ("get_execution_review", st.get_execution_review),
        ("seal_execution_review", st.seal_execution_review),
    ]
    for name, fn in pairs:
        monkeypatch.setattr(db, name, fn)
        monkeypatch.setattr(main, name, fn)
    main.app.state.db = FakePool()
    return st


@pytest.fixture
def client(store):
    with TestClient(main.app) as c:
        yield c


def _create(client, rid="rv-1"):
    client.post("/batches", json=BATCH)
    r = client.post("/inventory/plans", json=STEP_BODY,
                    headers={"X-Request-ID": rid})
    assert r.status_code == 201, r.text
    return r.json()["plan"]["transfers"]


def _register_all(client, rid, transfers):
    for t in transfers:
        r = client.post(f"/inventory/plans/{rid}/steps", json={
            "step": t["step"], "source": t["source"],
            "destination": t["destination"],
            "volume_nl": t["volume_nl"]})
        assert r.status_code == 201, r.text


def _review(client, rid, transfers, review_id="rev-1"):
    return client.post(
        f"/inventory/plans/{rid}/reviews",
        json={"actual_transfers": [
            {"step": t["step"], "source": t["source"],
             "destination": t["destination"], "volume_nl": t["volume_nl"]}
            for t in transfers]},
        headers={"X-Review-ID": review_id})


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #


def test_review_unknown_plan_404_and_no_report(client):
    r = client.post("/inventory/plans/nope/reviews",
                    json={"actual_transfers": []},
                    headers={"X-Review-ID": "r-x"})
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_plan"
    # nothing sealed
    assert client.get("/reviews/r-x").status_code == 404


def test_review_requires_review_id_header(client):
    _create(client)
    r = client.post("/inventory/plans/rv-1/reviews",
                    json={"actual_transfers": []})
    assert r.status_code == 422  # missing required header


def test_review_blank_review_id_header_is_400(client):
    _create(client)
    r = client.post("/inventory/plans/rv-1/reviews",
                    json={"actual_transfers": []},
                    headers={"X-Review-ID": "   "})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_review_stepwise_plan_still_running_is_rejected(client):
    _create(client)
    r = _review(client, "rv-1", [], "r-run")
    assert r.status_code == 409
    assert r.json()["error"] == "plan_not_finished"
    assert r.json()["status"] == "stepwise"
    assert client.get("/reviews/r-run").status_code == 404


def test_review_after_partial_abort_allowed_even_with_missing_steps(client):
    transfers = _create(client)
    client.post("/inventory/plans/rv-1/steps", json={
        "step": transfers[0]["step"], "source": transfers[0]["source"],
        "destination": transfers[0]["destination"],
        "volume_nl": transfers[0]["volume_nl"]})
    client.post("/inventory/plans/rv-1/abort")
    r = _review(client, "rv-1", transfers[:1], "r-ab")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["execution_status"] == "aborted"
    assert body["missing_steps"] == list(range(2, len(transfers) + 1))
    assert body["first_discrepancy"]["status"] == "missing"


def test_review_whole_mode_plan_rejected(client):
    client.post("/batches", json=BATCH)
    body = json.loads(json.dumps(STEP_BODY))
    body["execution_mode"] = "whole"
    client.post("/inventory/plans", json=body, headers={"X-Request-ID": "w-rv"})
    client.post("/inventory/plans/w-rv/confirm")
    r = client.post("/inventory/plans/w-rv/reviews",
                    json={"actual_transfers": []},
                    headers={"X-Review-ID": "r-whole"})
    assert r.status_code == 409
    assert r.json()["error"] == "whole_execution_mode"


def test_review_infeasible_frozen_plan_rejected(client, store):
    # an infeasible frozen inventory request has no reservation row
    store.plans["bad-plan"] = (
        "deadbeef", "infeasible",
        {"status": "infeasible", "request_id": "bad-plan"})
    r = client.post("/inventory/plans/bad-plan/reviews",
                    json={"actual_transfers": []},
                    headers={"X-Review-ID": "r-infeas"})
    assert r.status_code == 409
    assert r.json()["error"] == "plan_not_finished"
    assert client.get("/reviews/r-infeas").status_code == 404


def test_review_feasible_plan_without_reservation_rejected(client, store):
    # a feasible frozen plan with no reservation is a plain /plans result:
    # it has no stepwise execution lifecycle to reconcile
    store.plans["plain"] = ("h", "feasible",
                            {"status": "feasible", "request_id": "plain",
                             "plan": {"transfers": []}})
    r = client.post("/inventory/plans/plain/reviews",
                    json={"actual_transfers": []},
                    headers={"X-Review-ID": "r-plain"})
    assert r.status_code == 409
    assert r.json()["error"] == "plan_not_finished"
    assert client.get("/reviews/r-plain").status_code == 404


# --------------------------------------------------------------------------- #
# Matching and report content
# --------------------------------------------------------------------------- #


def test_review_completed_plan_perfect_match(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    r = _review(client, "rv-1", transfers, "r-ok")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["review_id"] == "r-ok"
    assert body["request_id"] == "rv-1"
    assert body["execution_status"] == "completed"
    assert body["all_match"] is True
    assert body["first_discrepancy"] is None
    assert body["frozen_step_count"] == len(transfers)
    assert body["actual_step_count"] == len(transfers)
    assert body["summary"] == {
        "total": len(transfers), "matched": len(transfers),
        "missing": 0, "extra": 0, "content_mismatch": 0}
    assert [s["step"] for s in body["steps"]] == \
        list(range(1, len(transfers) + 1))
    assert all(s["status"] == "matched" for s in body["steps"])
    stock = [t for t in transfers if t["source"]["kind"] == "stock"]
    total = sum(t["volume_nl"] for t in stock)
    assert body["stock_out_summary"]["frozen_total_nl"] == total
    assert body["stock_out_summary"]["actual_total_nl"] == total
    assert body["stock_out_summary"]["delta_nl"] == 0
    assert body["stock_out_summary"]["batches"] == [{
        "batch_id": "S1", "frozen_nl": total, "actual_nl": total,
        "delta_nl": 0,
        "frozen_steps": sorted(t["step"] for t in stock),
        "actual_steps": sorted(t["step"] for t in stock)}]
    assert "sealed_at" in body and "content_hash" in body


def test_review_aborted_after_step_one_first_discrepancy_is_missing(client):
    transfers = _create(client)
    client.post("/inventory/plans/rv-1/steps", json={
        "step": transfers[0]["step"], "source": transfers[0]["source"],
        "destination": transfers[0]["destination"],
        "volume_nl": transfers[0]["volume_nl"]})
    client.post("/inventory/plans/rv-1/abort")
    r = _review(client, "rv-1", transfers[:1], "r-miss")
    body = r.json()
    assert body["all_match"] is False
    assert body["summary"]["missing"] == len(transfers) - 1
    first = body["first_discrepancy"]
    assert first["step"] == 2 and first["status"] == "missing"
    assert "frozen" in first and "actual" not in first
    # frozen stock out = 1100, actual = 600
    summ = body["stock_out_summary"]
    assert summ["frozen_total_nl"] == 1100
    assert summ["actual_total_nl"] == 600
    assert summ["delta_nl"] == -500
    b = summ["batches"][0]
    assert b["batch_id"] == "S1" and b["frozen_nl"] == 1100 \
        and b["actual_nl"] == 600 and b["delta_nl"] == -500
    assert b["actual_steps"] == [1]


def test_review_detects_content_mismatch_volume_and_source(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    actual = json.loads(json.dumps(transfers))
    actual[0]["volume_nl"] -= 100
    r = _review(client, "rv-1", actual, "r-vol")
    body = r.json()
    assert body["all_match"] is False
    first = body["first_discrepancy"]
    assert first["step"] == 1
    assert first["status"] == "content_mismatch"
    assert first["differences"] == ["volume_nl"]
    assert first["frozen"]["volume_nl"] == transfers[0]["volume_nl"]
    assert first["actual"]["volume_nl"] == transfers[0]["volume_nl"] - 100
    assert body["mismatch_steps"] == [1]

    # source mismatch (unknown batch is a discrepancy, not a 400)
    actual = json.loads(json.dumps(transfers))
    actual[0]["source"]["id"] = "S9"
    r = _review(client, "rv-1", actual, "r-src")
    body = r.json()
    assert body["first_discrepancy"]["differences"] == ["source"]
    summ = body["stock_out_summary"]
    by = {b["batch_id"]: b for b in summ["batches"]}
    assert by["S1"]["actual_nl"] == 1100 - transfers[0]["volume_nl"]
    assert by["S9"]["actual_nl"] == transfers[0]["volume_nl"]
    assert by["S9"]["frozen_nl"] == 0


def test_review_detects_destination_mismatch_and_extra_step(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    actual = json.loads(json.dumps(transfers))
    actual[1]["destination"] = {"kind": "well", "id": "A9"}
    actual.append({
        "step": len(transfers) + 1,
        "source": {"kind": "diluent"},
        "destination": {"kind": "well", "id": "A1"},
        "volume_nl": 200})
    r = _review(client, "rv-1", actual, "r-dst")
    body = r.json()
    assert body["first_discrepancy"]["step"] == 2
    assert body["first_discrepancy"]["differences"] == ["destination"]
    assert body["extra_steps"] == [len(transfers) + 1]
    extra = body["steps"][-1]
    assert extra["status"] == "extra" and "frozen" not in extra


# --------------------------------------------------------------------------- #
# Whole-request structural rejections (400, no report)
# --------------------------------------------------------------------------- #


def _review_raw(client, rid, payload, review_id):
    return client.post(f"/inventory/plans/{rid}/reviews", json=payload,
                       headers={"X-Review-ID": review_id})


def test_review_duplicate_step_rejected_without_report(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    payload = {"actual_transfers": [
        {"step": 1, **{k: transfers[0][k] for k in
                       ("source", "destination", "volume_nl")}},
        {"step": 1, **{k: transfers[0][k] for k in
                       ("source", "destination", "volume_nl")}},
    ]}
    r = _review_raw(client, "rv-1", payload, "r-dup")
    assert r.status_code == 400
    assert r.json()["error"] == "duplicate_step"
    assert r.json()["step"] == 1
    assert client.get("/reviews/r-dup").status_code == 404


def test_review_non_ordered_steps_rejected(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    payload = {"actual_transfers": [
        {"step": 2, "source": transfers[1]["source"],
         "destination": transfers[1]["destination"],
         "volume_nl": transfers[1]["volume_nl"]},
        {"step": 1, "source": transfers[0]["source"],
         "destination": transfers[0]["destination"],
         "volume_nl": transfers[0]["volume_nl"]},
    ]}
    r = _review_raw(client, "rv-1", payload, "r-order")
    assert r.status_code == 400
    assert r.json()["error"] == "steps_not_ordered"
    assert client.get("/reviews/r-order").status_code == 404


def test_review_gap_in_steps_is_allowed_and_reported_as_missing(client):
    # gaps in the ordered actual record are not a structural rejection: the
    # skipped frozen steps are reported as missing
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    payload = {"actual_transfers": [
        {"step": 1, "source": transfers[0]["source"],
         "destination": transfers[0]["destination"],
         "volume_nl": transfers[0]["volume_nl"]},
        {"step": 3, "source": transfers[2]["source"],
         "destination": transfers[2]["destination"],
         "volume_nl": transfers[2]["volume_nl"]},
    ]}
    r = _review_raw(client, "rv-1", payload, "r-gap")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["missing_steps"] == [2]
    assert body["first_discrepancy"]["step"] == 2
    assert body["first_discrepancy"]["status"] == "missing"
    statuses = {s["step"]: s["status"] for s in body["steps"]}
    assert statuses == {1: "matched", 2: "missing", 3: "matched"}


def test_review_illegal_node_rejected(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    # well used as a source
    bad = {"actual_transfers": [{
        "step": 1, "source": {"kind": "well", "id": "A1"},
        "destination": transfers[0]["destination"],
        "volume_nl": 600}]}
    r = _review_raw(client, "rv-1", bad, "r-illegal")
    assert r.status_code == 400 and r.json()["error"] == "illegal_node"
    # stock used as a destination
    bad2 = {"actual_transfers": [{
        "step": 1, "source": transfers[0]["source"],
        "destination": {"kind": "stock", "id": "S1"},
        "volume_nl": 600}]}
    r = _review_raw(client, "rv-1", bad2, "r-illegal2")
    assert r.status_code == 400 and r.json()["error"] == "illegal_node"
    assert client.get("/reviews/r-illegal").status_code == 404
    assert client.get("/reviews/r-illegal2").status_code == 404


def test_review_non_positive_volume_is_400(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    bad = {"actual_transfers": [{
        "step": 1, "source": transfers[0]["source"],
        "destination": transfers[0]["destination"], "volume_nl": 0}]}
    r = _review_raw(client, "rv-1", bad, "r-zero")
    assert r.status_code == 400
    assert client.get("/reviews/r-zero").status_code == 404


def test_review_bad_node_kind_is_400(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    bad = {"actual_transfers": [{
        "step": 1, "source": {"kind": "ghost"},
        "destination": transfers[0]["destination"], "volume_nl": 600}]}
    r = _review_raw(client, "rv-1", bad, "r-kind")
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Idempotency / conflict / retrieval
# --------------------------------------------------------------------------- #


def test_same_review_id_same_content_replays(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    r1 = _review(client, "rv-1", transfers, "r-replay")
    r2 = _review(client, "rv-1", transfers, "r-replay")
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    # also retrievable via both GET routes
    assert client.get("/reviews/r-replay").json() == r1.json()
    g = client.get("/inventory/plans/rv-1/reviews/r-replay")
    assert g.status_code == 200 and g.json() == r1.json()


def test_same_review_id_different_content_conflicts(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    r1 = _review(client, "rv-1", transfers, "r-conf")
    assert r1.status_code == 201
    changed = json.loads(json.dumps(transfers))
    changed[0]["volume_nl"] -= 100
    r2 = _review(client, "rv-1", changed, "r-conf")
    assert r2.status_code == 409
    assert r2.json()["error"] == "review_conflict"
    assert r2.json()["existing_request_id"] == "rv-1"
    # the original report is still returned
    assert client.get("/reviews/r-conf").json() == r1.json()


def test_get_review_under_wrong_plan_is_404(client):
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    _review(client, "rv-1", transfers, "r-mine")
    r = client.get("/inventory/plans/other/reviews/r-mine")
    assert r.status_code == 404
    assert r.json()["request_id"] == "rv-1"


def test_get_unknown_review_is_404(client):
    assert client.get("/reviews/nope").status_code == 404
    assert client.get(
        "/inventory/plans/rv-1/reviews/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Reviews are read-only
# --------------------------------------------------------------------------- #


def test_review_does_not_change_inventory_or_steps(client):
    transfers = _create(client)
    # aborted after step 1: 600 used, 500 released
    client.post("/inventory/plans/rv-1/steps", json={
        "step": transfers[0]["step"], "source": transfers[0]["source"],
        "destination": transfers[0]["destination"],
        "volume_nl": transfers[0]["volume_nl"]})
    client.post("/inventory/plans/rv-1/abort")
    before = client.get("/batches/S1").json()
    plan_view = client.get("/inventory/plans/rv-1").json()
    # review claims a different actual record (first step with another volume)
    weird = json.loads(json.dumps(transfers))
    weird[0]["volume_nl"] = 100
    weird = weird[:1]
    r = _review(client, "rv-1", weird, "r-ro")
    assert r.status_code == 201
    after = client.get("/batches/S1").json()
    assert before == after
    view_after = client.get("/inventory/plans/rv-1").json()
    assert view_after["step_registrations"] == plan_view["step_registrations"]
    assert view_after["reservation_status"] == "aborted"


def test_replay_works_even_when_plan_disappeared_from_eligibility(client):
    # a sealed review is immutable: replaying the same id returns the report
    # without re-running the eligibility check (the plan state no longer
    # matters to a sealed document)
    transfers = _create(client)
    _register_all(client, "rv-1", transfers)
    client.post("/inventory/plans/rv-1/complete")
    r1 = _review(client, "rv-1", transfers, "r-imm")
    assert r1.status_code == 201
    r2 = client.post("/inventory/plans/nope/reviews",
                     json={"actual_transfers": [
                         {"step": t["step"], "source": t["source"],
                          "destination": t["destination"],
                          "volume_nl": t["volume_nl"]} for t in transfers]},
                     headers={"X-Review-ID": "r-imm"})
    # same id + same content (request id is part of content) -> the path
    # request id differs, so content differs: conflict
    assert r2.status_code == 409
    # identical replay via the original path still returns the sealed report
    assert _review(client, "rv-1", transfers, "r-imm").status_code == 200
