#!/usr/bin/env python3
"""Execution-variance reconciliation review demo (standard library only).

A review compares the frozen plan of an inventory stepwise plan that has
already finished (completed or aborted) with the full ordered record of the
actual transfers, and seals an immutable report under an X-Review-ID key:

* per-step results: matched / missing / extra / content_mismatch, plus the
  first discrepancy and frozen vs actual stock-out totals per batch;
* same review id + same content replays the original sealed report (200);
* same review id + different content conflicts (409);
* unknown plan, unfinished plan, duplicate steps, illegal nodes and
  non-positive volumes are rejected whole (no report sealed);
* a review never changes inventory, the plan or registered steps.

Usage:
    python examples/review_demo.py [base_url]
Default base URL: http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

with open("examples/inventory_stepwise.json", encoding="utf-8") as fh:
    PLAN_BODY = json.load(fh)


def call(path, body=None, review_id=None, request_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if review_id:
        headers["X-Review-ID"] = review_id
    if request_id:
        headers["X-Request-ID"] = request_id
    data = json.dumps(body).encode() if body is not None else None
    if method is None:
        method = "POST" if body is not None else "GET"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def as_record(transfers):
    return {"actual_transfers": [
        {"step": t["step"], "source": t["source"],
         "destination": t["destination"], "volume_nl": t["volume_nl"]}
        for t in transfers]}


def run_plan(rid):
    call("/batches", {"id": "S1", "concentration": "1000",
                      "initial_volume_nl": 1_000_000})
    status, doc = call("/inventory/plans", PLAN_BODY, request_id=rid)
    assert status == 201, (status, doc)
    return doc["plan"]["transfers"]


print("1) plan A is fully executed and completed; review matches exactly:")
rid_a = "review-py-a"
transfers = run_plan(rid_a)
for t in transfers:
    code, _ = call(f"/inventory/plans/{rid_a}/steps", {
        "step": t["step"], "source": t["source"],
        "destination": t["destination"], "volume_nl": t["volume_nl"]})
    assert code == 201, code
call(f"/inventory/plans/{rid_a}/complete", body={})
code, report = call(f"/inventory/plans/{rid_a}/reviews", as_record(transfers),
                    review_id="review-A")
print("   seal -> HTTP", code, "all_match:", report["all_match"])
print("   per-step:", [(s["step"], s["status"]) for s in report["steps"]])
print("   stock out:", report["stock_out_summary"]["batches"])

print("2) same review id + same content replays the sealed report (200):")
code, again = call(f"/inventory/plans/{rid_a}/reviews", as_record(transfers),
                   review_id="review-A")
print("   HTTP", code, "identical:", again == report)

print("3) same review id with different content conflicts (409):")
changed = json.loads(json.dumps(transfers))
changed[0]["volume_nl"] -= 100
code, body = call(f"/inventory/plans/{rid_a}/reviews", as_record(changed),
                  review_id="review-A")
print("   HTTP", code, body["error"])

print("4) a wrong volume on step 1 is reported as content_mismatch:")
code, report = call(f"/inventory/plans/{rid_a}/reviews", as_record(changed),
                    review_id="review-A-badvolume")
fd = report["first_discrepancy"]
print("   HTTP", code, "first:", fd["step"], fd["status"],
      "differences:", fd["differences"])
print("   summary:", report["summary"])
print("   frozen vs actual stock:",
      report["stock_out_summary"]["batches"])

print("5) plan B is aborted after step 1: later steps are missing:")
rid_b = "review-py-b"
transfers_b = run_plan(rid_b)
first = transfers_b[0]
call(f"/inventory/plans/{rid_b}/steps", {
    "step": first["step"], "source": first["source"],
    "destination": first["destination"], "volume_nl": first["volume_nl"]})
call(f"/inventory/plans/{rid_b}/abort", body={})
code, report = call(f"/inventory/plans/{rid_b}/reviews",
                    as_record(transfers_b[:1]), review_id="review-B")
print("   HTTP", code, "status:", report["execution_status"])
print("   missing steps:", report["missing_steps"],
      "first discrepancy:", report["first_discrepancy"]["step"],
      report["first_discrepancy"]["status"])
print("   stock out frozen/actual/delta nL:",
      report["stock_out_summary"]["frozen_total_nl"],
      report["stock_out_summary"]["actual_total_nl"],
      report["stock_out_summary"]["delta_nl"])

print("6) reviews are read-only and retrievable later:")
_, batch_before = call("/batches/S1")
call(f"/inventory/plans/{rid_b}/reviews", as_record(transfers_b[:1]),
     review_id="review-B")  # same content -> 200 replay
_, batch_after = call("/batches/S1")
print("   batch unchanged by review:", batch_before == batch_after)
code, fetched = call("/reviews/review-B")
print("   GET /reviews/review-B -> HTTP", code,
      "request_id:", fetched["request_id"], "sealed_at:",
      fetched["sealed_at"])

print("7) whole-request rejections seal no report:")
# still-running plan
rid_c = "review-py-c"
run_plan(rid_c)
code, body = call(f"/inventory/plans/{rid_c}/reviews", as_record([]),
                  review_id="review-C-running")
print("   unfinished plan -> HTTP", code, body["error"])
# duplicate step number in the submitted record
dup = {"actual_transfers": [
    {"step": first["step"], "source": first["source"],
     "destination": first["destination"], "volume_nl": first["volume_nl"]},
    {"step": first["step"], "source": first["source"],
     "destination": first["destination"], "volume_nl": first["volume_nl"]}]}
code, body = call(f"/inventory/plans/{rid_b}/reviews", dup,
                  review_id="review-C-dup")
print("   duplicate step -> HTTP", code, body["error"])
# illegal node (well used as a source)
illegal = {"actual_transfers": [
    {"step": 1, "source": {"kind": "well", "id": "A1"},
     "destination": first["destination"], "volume_nl": 500}]}
code, body = call(f"/inventory/plans/{rid_b}/reviews", illegal,
                  review_id="review-C-illegal")
print("   illegal node -> HTTP", code, body["error"])
code, body = call("/reviews/review-C-dup")
print("   no report sealed for duplicate -> GET HTTP", code)
