"""Execution-variance review tests.

A finished inventory plan (whole-mode ``confirmed`` or stepwise
``completed`` / ``aborted``) accepts a review: the submitter posts the plan
id, the full ordered actual transfer record (step, source, destination,
volume) and a unique review id; the service compares the record step by step
against the frozen plan and seals an immutable report.

Rules under test:

* per-step match results (matched / missing / content_mismatch), the first
  missing / extra / mismatch position, and the frozen-vs-actual stock-out
  summary per batch;
* the sealed report never changes inventory, plans or registered steps;
* same review id + same content replays the original report (200); same
  review id + different content conflicts (409);
* unknown plan (404), not-yet-ended plan (409), duplicate steps (400),
  illegal nodes (400) and non-positive volumes (400) reject the whole
  submission and seal nothing (the review id stays reusable);
* reviews are readable back via GET /reviews/{review_id}.
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.planning import compare_actual_transfers, review_hash
from tests.test_inventory import FakePool, FakeStore
import app.db as db
import app.main as main


@pytest.fixture
def store(monkeypatch):
    st = FakeStore()

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
        ("create_review", st.create_review),
        ("get_review", st.get_review),
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


@pytest.fixture
def anyio_backend():
    return "asyncio"


BATCH = {"id": "S1", "concentration": "1000", "initial_volume_nl": 1_000_000}

# Two-well serial dilution: stock S1 -> intermediates -> wells A1/A2.
STEP_BODY = {
    "batch_ids": ["S1"],
    "diluent": {"available_nl": 10_000_000},
    "wells": [
        {"id": "A1", "final_volume_nl": 1000, "final_concentration": "400"},
        {"id": "A2", "final_volume_nl": 1000, "final_concentration": "100"},
    ],
    "containers": [
        {"id": "tube-1", "capacity_nl": 10_000},
        {"id": "tube-2", "capacity_nl": 20_000},
    ],
    "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
    "default_well_capacity_nl": 2000,
    "execution_mode": "stepwise",
}


def _create(client, rid, body=None):
    return client.post("/inventory/plans", json=body or STEP_BODY,
                       headers={"X-Request-ID": rid})


def _transfers(client, rid):
    return client.get(f"/inventory/plans/{rid}").json()["plan"]["transfers"]


def _actual(transfers):
    """Actual record identical to the given frozen transfers."""
    return [{"step": t["step"], "source": t["source"],
             "destination": t["destination"], "volume_nl": t["volume_nl"]}
            for t in transfers]


def _review(client, rid, review_id, actual):
    return client.post(f"/inventory/plans/{rid}/review",
                       json={"actual_transfers": actual},
                       headers={"X-Review-ID": review_id})


def _register(client, rid, transfer):
    return client.post(f"/inventory/plans/{rid}/steps", json={
        "step": transfer["step"],
        "source": transfer["source"],
        "destination": transfer["destination"],
        "volume_nl": transfer["volume_nl"],
    })


def _completed_plan(client, rid="rv-1"):
    """A fully executed + completed stepwise plan; returns its transfers."""
    client.post("/batches", json=BATCH)
    _create(client, rid)
    transfers = _transfers(client, rid)
    for t in transfers:
        assert _register(client, rid, t).status_code == 201
    assert client.post(f"/inventory/plans/{rid}/complete").status_code == 201
    return transfers


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def _plan(transfers):
    return {"transfers": transfers}


def _t(step, src_kind="stock", src_id="S1", dst_kind="well", dst_id="A1",
       volume=600, container=None):
    src = {"kind": src_kind, "id": src_id}
    if container is not None:
        src["container"] = container
    return {"step": step, "source": src,
            "destination": {"kind": dst_kind, "id": dst_id},
            "volume_nl": volume}


def test_compare_exact_match():
    plan = _plan([_t(1), _t(2, src_kind="diluent", src_id=None, volume=900)])
    out = compare_actual_transfers(plan, _actual(plan["transfers"]))
    assert out["matched"] is True
    assert out["summary"] == {
        "total_steps": 2, "actual_records": 2, "matched_steps": 2,
        "missing_steps": 0, "mismatched_steps": 0, "extra_records": 0}
    assert [s["status"] for s in out["steps"]] == ["matched", "matched"]
    assert out["first_missing_step"] is None
    assert out["first_mismatch_step"] is None
    assert out["first_extra_step"] is None
    assert out["stock_summary"] == [
        {"batch_id": "S1", "frozen_nl": 600, "actual_nl": 600,
         "difference_nl": 0}]


def test_compare_missing_mismatch_extra_and_first_positions():
    frozen = [_t(1), _t(2, volume=900), _t(3, volume=500), _t(4, volume=700)]
    actual = [
        _t(1),                       # matched
        _t(3, volume=100),           # content mismatch (volume)
        _t(5, volume=100),           # extra
        _t(6, volume=100),           # extra
    ]
    out = compare_actual_transfers(_plan(frozen), actual)
    assert out["matched"] is False
    assert [s["status"] for s in out["steps"]] == [
        "matched", "missing", "content_mismatch", "missing"]
    assert out["steps"][2]["differences"] == ["volume_nl"]
    assert out["first_missing_step"] == 2
    assert out["first_mismatch_step"] == 3
    assert out["first_extra_step"] == 5
    assert [e["step"] for e in out["extra_records"]] == [5, 6]
    assert out["summary"]["missing_steps"] == 2
    assert out["summary"]["mismatched_steps"] == 1
    assert out["summary"]["extra_records"] == 2
    # stock-out summary: frozen counts every frozen stock step, actual counts
    # the submitted records (matched + mismatched + extra alike)
    s1 = next(r for r in out["stock_summary"] if r["batch_id"] == "S1")
    assert s1["frozen_nl"] == 600 + 900 + 500 + 700
    assert s1["actual_nl"] == 600 + 100 + 100 + 100
    assert s1["difference_nl"] == s1["actual_nl"] - s1["frozen_nl"]


def test_compare_flags_source_and_destination_differences():
    frozen = [_t(1, container="tube-1")]
    actual = [{"step": 1,
               "source": {"kind": "stock", "id": "S2", "container": None},
               "destination": {"kind": "well", "id": "A2",
                               "container": None},
               "volume_nl": 600}]
    out = compare_actual_transfers(_plan(frozen), actual)
    assert out["steps"][0]["status"] == "content_mismatch"
    assert out["steps"][0]["differences"] == ["source", "destination"]
    # the foreign stock id shows up in the summary with a zero frozen side
    s2 = next(r for r in out["stock_summary"] if r["batch_id"] == "S2")
    assert (s2["frozen_nl"], s2["actual_nl"], s2["difference_nl"]) \
        == (0, 600, 600)


def test_review_hash_normalizes_record_order():
    a = [_t(1), _t(2, volume=900)]
    b = list(reversed(a))
    assert review_hash("p1", a) == review_hash("p1", b)
    assert review_hash("p1", a) != review_hash("p2", a)
    changed = [_t(1), _t(2, volume=800)]
    assert review_hash("p1", a) != review_hash("p1", changed)


# --------------------------------------------------------------------------- #
# Happy path: completed / aborted / confirmed plans
# --------------------------------------------------------------------------- #


def test_review_completed_stepwise_plan_exact_match(client):
    transfers = _completed_plan(client)
    r = _review(client, "rv-1", "review-1", _actual(transfers))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["review_id"] == "review-1"
    assert body["request_id"] == "rv-1"
    assert body["reviewed_at"]
    report = body["report"]
    assert report["matched"] is True
    assert report["execution_mode"] == "stepwise"
    assert report["reservation_status"] == "completed"
    assert report["summary"]["total_steps"] == len(transfers)
    assert report["summary"]["matched_steps"] == len(transfers)
    assert all(s["status"] == "matched" for s in report["steps"])
    assert report["stock_summary"] == [
        {"batch_id": "S1", "frozen_nl": 1100, "actual_nl": 1100,
         "difference_nl": 0}]


def test_review_aborted_plan_reports_missing_steps(client):
    client.post("/batches", json=BATCH)
    _create(client, "rv-ab")
    transfers = _transfers(client, "rv-ab")
    _register(client, "rv-ab", transfers[0])  # 600 nL stock-out, then abort
    assert client.post("/inventory/plans/rv-ab/abort").status_code == 201

    r = _review(client, "rv-ab", "review-ab", _actual(transfers[:1]))
    assert r.status_code == 201, r.text
    report = r.json()["report"]
    assert report["reservation_status"] == "aborted"
    assert report["matched"] is False
    assert report["steps"][0]["status"] == "matched"
    assert [s["status"] for s in report["steps"][1:]] \
        == ["missing"] * (len(transfers) - 1)
    assert report["first_missing_step"] == 2
    assert report["stock_summary"] == [
        {"batch_id": "S1", "frozen_nl": 1100, "actual_nl": 600,
         "difference_nl": -500}]


def test_review_confirmed_whole_mode_plan(client):
    client.post("/batches", json=BATCH)
    body = json.loads(json.dumps(STEP_BODY))
    body["execution_mode"] = "whole"
    _create(client, "rv-whole", body)
    assert client.post("/inventory/plans/rv-whole/confirm").status_code == 200
    transfers = _transfers(client, "rv-whole")
    r = _review(client, "rv-whole", "review-whole", _actual(transfers))
    assert r.status_code == 201, r.text
    report = r.json()["report"]
    assert report["execution_mode"] == "whole"
    assert report["reservation_status"] == "confirmed"
    assert report["matched"] is True


def test_review_empty_actual_record_on_aborted_plan(client):
    client.post("/batches", json=BATCH)
    _create(client, "rv-empty")
    transfers = _transfers(client, "rv-empty")
    assert client.post("/inventory/plans/rv-empty/abort").status_code == 201
    r = _review(client, "rv-empty", "review-empty", [])
    assert r.status_code == 201, r.text
    report = r.json()["report"]
    assert report["matched"] is False
    assert report["summary"]["missing_steps"] == len(transfers)
    assert report["first_missing_step"] == 1
    assert report["stock_summary"] == [
        {"batch_id": "S1", "frozen_nl": 1100, "actual_nl": 0,
         "difference_nl": -1100}]


# --------------------------------------------------------------------------- #
# Idempotency and immutability
# --------------------------------------------------------------------------- #


def test_same_review_id_same_content_replays_original(client):
    transfers = _completed_plan(client)
    first = _review(client, "rv-1", "review-re", _actual(transfers))
    again = _review(client, "rv-1", "review-re", _actual(transfers))
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == first.json()
    # submission order of the records does not matter for identity
    shuffled = _actual(list(reversed(transfers)))
    reordered = _review(client, "rv-1", "review-re", shuffled)
    assert reordered.status_code == 200
    assert reordered.json() == first.json()
    # exactly one sealed report exists
    assert client.get("/reviews/review-re").json() == first.json()


def test_same_review_id_different_content_conflicts(client):
    transfers = _completed_plan(client)
    assert _review(client, "rv-1", "review-cf",
                   _actual(transfers)).status_code == 201
    changed = _actual(transfers)
    changed[0]["volume_nl"] -= 100
    r = _review(client, "rv-1", "review-cf", changed)
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_conflict"
    # the sealed report is untouched
    assert client.get("/reviews/review-cf").json()["report"]["matched"] is True


def test_same_review_id_different_plan_conflicts(client):
    transfers = _completed_plan(client)
    _completed_plan(client, "rv-2")
    assert _review(client, "rv-1", "review-x",
                   _actual(transfers)).status_code == 201
    r = _review(client, "rv-2", "review-x", _actual(transfers))
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_conflict"


def test_review_does_not_touch_inventory_plan_or_registrations(client):
    transfers = _completed_plan(client)
    before_batch = client.get("/batches/S1").json()
    before_plan = client.get("/inventory/plans/rv-1").json()
    # a diverging actual record: the review reports it but changes nothing
    actual = _actual(transfers)
    actual[0]["volume_nl"] -= 100
    r = _review(client, "rv-1", "review-ro", actual)
    assert r.status_code == 201
    assert r.json()["report"]["matched"] is False
    assert client.get("/batches/S1").json() == before_batch
    assert client.get("/inventory/plans/rv-1").json() == before_plan


def test_get_unknown_review_is_404(client):
    assert client.get("/reviews/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Whole-batch rejections seal nothing
# --------------------------------------------------------------------------- #


def test_unknown_plan_rejected_and_review_id_stays_reusable(client):
    actual = [{"step": 1, "source": {"kind": "diluent"},
               "destination": {"kind": "well", "id": "A1"},
               "volume_nl": 100}]
    r = _review(client, "ghost-plan", "review-g", actual)
    assert r.status_code == 404
    assert client.get("/reviews/review-g").status_code == 404
    # a non-inventory (/plans) request id is equally not reviewable
    client.post("/plans", json={
        "stocks": [{"id": "S9", "concentration": "100",
                    "available_nl": 1_000_000}],
        "diluent": {"available_nl": 10_000_000},
        "wells": [{"id": "A1", "final_volume_nl": 600,
                   "final_concentration": "100"}],
        "containers": [],
        "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
        "default_well_capacity_nl": 5000,
    }, headers={"X-Request-ID": "plain-1"})
    assert _review(client, "plain-1", "review-g", actual).status_code == 404
    # the rejection sealed nothing: the review id is still usable
    transfers = _completed_plan(client)
    ok = _review(client, "rv-1", "review-g", _actual(transfers))
    assert ok.status_code == 201


def test_infeasible_inventory_plan_is_not_reviewable(client):
    client.post("/batches", json=BATCH)
    body = json.loads(json.dumps(STEP_BODY))
    body["wells"][0]["final_concentration"] = "1500"  # above stock conc
    r = _create(client, "rv-infeasible", body)
    assert r.status_code == 422
    assert _review(client, "rv-infeasible", "review-inf",
                   []).status_code == 404
    assert client.get("/reviews/review-inf").status_code == 404


def test_unended_plans_rejected(client):
    client.post("/batches", json=BATCH)
    # stepwise plan still executing
    _create(client, "rv-open")
    transfers = _transfers(client, "rv-open")
    _register(client, "rv-open", transfers[0])
    r = _review(client, "rv-open", "review-open", _actual(transfers))
    assert r.status_code == 409
    assert r.json()["error"] == "plan_not_ended"
    assert r.json()["status"] == "stepwise"
    # whole-mode plan still reserved
    body = json.loads(json.dumps(STEP_BODY))
    body["execution_mode"] = "whole"
    _create(client, "rv-pending", body)
    r = _review(client, "rv-pending", "review-pend", _actual(transfers))
    assert r.status_code == 409
    assert r.json()["error"] == "plan_not_ended"
    assert r.json()["status"] == "reserved"
    # cancelled whole-mode plan: ended but never executed
    client.post("/inventory/plans/rv-pending/cancel")
    r = _review(client, "rv-pending", "review-pend", _actual(transfers))
    assert r.status_code == 409
    assert r.json()["error"] == "plan_not_ended"
    assert r.json()["status"] == "cancelled"
    # nothing was sealed by any of the rejections
    assert client.get("/reviews/review-open").status_code == 404
    assert client.get("/reviews/review-pend").status_code == 404
    # once the plan ends, the same review id succeeds
    for t in transfers[1:]:
        _register(client, "rv-open", t)
    client.post("/inventory/plans/rv-open/complete")
    ok = _review(client, "rv-open", "review-open", _actual(transfers))
    assert ok.status_code == 201
    assert ok.json()["report"]["matched"] is True


def test_duplicate_steps_rejected(client):
    transfers = _completed_plan(client)
    actual = _actual(transfers)
    actual.append(dict(actual[0]))  # step 1 twice
    r = _review(client, "rv-1", "review-dup", actual)
    assert r.status_code == 400
    assert r.json()["error"] == "duplicate_step"
    assert r.json()["steps"] == [1]
    assert client.get("/reviews/review-dup").status_code == 404
    # the id is unconsumed: a corrected submission succeeds
    ok = _review(client, "rv-1", "review-dup", _actual(transfers))
    assert ok.status_code == 201


def test_illegal_node_and_nonpositive_volume_rejected(client):
    transfers = _completed_plan(client)
    good = _actual(transfers)

    bad_kind = _actual(transfers)
    bad_kind[0]["source"] = {"kind": "ghost", "id": "S1"}
    assert _review(client, "rv-1", "review-bad", bad_kind).status_code == 400

    zero_vol = _actual(transfers)
    zero_vol[0]["volume_nl"] = 0
    assert _review(client, "rv-1", "review-bad", zero_vol).status_code == 400

    neg_vol = _actual(transfers)
    neg_vol[0]["volume_nl"] = -100
    assert _review(client, "rv-1", "review-bad", neg_vol).status_code == 400

    zero_step = _actual(transfers)
    zero_step[0]["step"] = 0
    assert _review(client, "rv-1", "review-bad", zero_step).status_code == 400

    assert client.get("/reviews/review-bad").status_code == 404
    assert _review(client, "rv-1", "review-bad", good).status_code == 201


def test_missing_or_blank_review_id_header(client):
    transfers = _completed_plan(client)
    r = client.post("/inventory/plans/rv-1/review",
                    json={"actual_transfers": _actual(transfers)})
    assert r.status_code == 400
    r = client.post("/inventory/plans/rv-1/review",
                    json={"actual_transfers": _actual(transfers)},
                    headers={"X-Review-ID": "   "})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Divergence reporting
# --------------------------------------------------------------------------- #


def test_content_mismatch_reports_differing_fields(client):
    transfers = _completed_plan(client)
    # steps 1 and 3 are the stock-out steps (600 + 500 nL of S1)
    assert transfers[0]["source"]["kind"] == "stock"
    actual = _actual(transfers)
    actual[0]["volume_nl"] -= 100                       # volume differs
    actual[2]["destination"] = {"kind": "well", "id": "A9",
                                "container": None}      # destination differs
    r = _review(client, "rv-1", "review-mm", actual)
    assert r.status_code == 201
    report = r.json()["report"]
    assert report["matched"] is False
    s1_step = report["steps"][0]
    assert s1_step["status"] == "content_mismatch"
    assert s1_step["differences"] == ["volume_nl"]
    assert s1_step["frozen"]["volume_nl"] == transfers[0]["volume_nl"]
    assert s1_step["actual"]["volume_nl"] == transfers[0]["volume_nl"] - 100
    s3 = report["steps"][2]
    assert s3["status"] == "content_mismatch"
    assert s3["differences"] == ["destination"]
    assert report["first_mismatch_step"] == 1
    assert report["summary"]["mismatched_steps"] == 2
    # stock summary reflects the actual (smaller) stock-out
    s1 = report["stock_summary"][0]
    assert s1["batch_id"] == "S1"
    assert s1["difference_nl"] == -100


def test_extra_records_reported(client):
    transfers = _completed_plan(client)
    actual = _actual(transfers)
    extra_step = len(transfers) + 1
    actual.append({"step": extra_step,
                   "source": {"kind": "stock", "id": "S2"},
                   "destination": {"kind": "well", "id": "A1"},
                   "volume_nl": 500})
    r = _review(client, "rv-1", "review-extra", actual)
    assert r.status_code == 201
    report = r.json()["report"]
    assert report["matched"] is False
    assert report["summary"]["extra_records"] == 1
    assert report["first_extra_step"] == extra_step
    assert report["extra_records"][0]["step"] == extra_step
    assert report["extra_records"][0]["source"]["id"] == "S2"
    # all frozen steps still matched; the foreign batch appears in the summary
    assert all(s["status"] == "matched" for s in report["steps"])
    s2 = next(x for x in report["stock_summary"]
              if x["batch_id"] == "S2")
    assert (s2["frozen_nl"], s2["actual_nl"], s2["difference_nl"]) \
        == (0, 500, 500)


def test_gap_in_actual_record_marks_missing_step(client):
    transfers = _completed_plan(client)
    actual = _actual([t for t in transfers if t["step"] != 2])
    r = _review(client, "rv-1", "review-gap", actual)
    assert r.status_code == 201
    report = r.json()["report"]
    assert report["steps"][1]["status"] == "missing"
    assert report["steps"][1]["actual"] is None
    assert report["first_missing_step"] == 2
    assert report["summary"]["missing_steps"] == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_concurrent_same_review_seals_once(store, anyio_backend):
    await store.create_batch(None, "S1", "1000", 1_000_000)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        created = await ac.post("/inventory/plans", json=STEP_BODY,
                                headers={"X-Request-ID": "rv-race"})
        assert created.status_code == 201
        transfers = created.json()["plan"]["transfers"]
        for t in transfers:
            resp = await ac.post("/inventory/plans/rv-race/steps", json={
                "step": t["step"], "source": t["source"],
                "destination": t["destination"],
                "volume_nl": t["volume_nl"]})
            assert resp.status_code == 201
        await ac.post("/inventory/plans/rv-race/complete")
        actual = _actual(transfers)

        async def review(review_id, records):
            return await ac.post("/inventory/plans/rv-race/review",
                                 json={"actual_transfers": records},
                                 headers={"X-Review-ID": review_id})

        # identical content races: exactly one seals, the rest replay
        results = await asyncio.gather(*[
            review("review-race", actual) for _ in range(6)])
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 200, 200, 200, 200, 201]
        bodies = {json.dumps(r.json(), sort_keys=True) for r in results}
        assert len(bodies) == 1

        # diverging content races on a fresh id: one seals, its twins replay,
        # the rest conflict
        changed = _actual(transfers)
        changed[0]["volume_nl"] -= 100
        results = await asyncio.gather(*[
            review("review-race2", actual if i % 2 == 0 else changed)
            for i in range(6)])
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 200, 201, 409, 409, 409]
        sealed = (await ac.get("/reviews/review-race2")).json()
        assert sealed["review_id"] == "review-race2"
        winner = next(r for r in results if r.status_code == 201)
        assert sealed == winner.json()
