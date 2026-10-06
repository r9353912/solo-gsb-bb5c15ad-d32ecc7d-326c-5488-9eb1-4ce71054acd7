#!/usr/bin/env python3
"""Execution-variance review demo using only the standard library.

Walks a stepwise plan to completion, then reviews the finished plan: the
submitter posts the full ordered actual transfer record under a unique
review id and gets back a sealed, immutable report comparing the record
step by step against the frozen plan. Also shows an aborted plan with
missing steps, a content mismatch, idempotent replay, the different-content
conflict, and the whole-batch rejections (unknown / unended plan, duplicate
steps) that seal nothing.

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


def call(path, body=None, request_id=None, review_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if request_id:
        headers["X-Request-ID"] = request_id
    if review_id:
        headers["X-Review-ID"] = review_id
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


def register(rid, t):
    return call(f"/inventory/plans/{rid}/steps", {
        "step": t["step"], "source": t["source"],
        "destination": t["destination"], "volume_nl": t["volume_nl"]})


def actual_of(transfers):
    """An actual record identical to the frozen transfers."""
    return [{"step": t["step"], "source": t["source"],
             "destination": t["destination"], "volume_nl": t["volume_nl"]}
            for t in transfers]


def review(rid, review_id, actual):
    return call(f"/inventory/plans/{rid}/review",
                {"actual_transfers": actual}, review_id=review_id)


print("1) register batch and run a stepwise plan to completion:")
status, _ = call("/batches", {"id": "S1", "concentration": "1000",
                              "initial_volume_nl": 1_000_000})
print("   batch -> HTTP", status)
rid = "review-py-1"
status, plan_doc = call("/inventory/plans", PLAN_BODY, request_id=rid)
transfers = plan_doc["plan"]["transfers"]
print("   plan -> HTTP", status, "steps:", len(transfers))
for t in transfers:
    register(rid, t)
status, body = call(f"/inventory/plans/{rid}/complete", body={})
print("   complete -> HTTP", status, body["status"])

print("2) review the completed plan with the exact actual record:")
status, sealed = review(rid, "review-py-001", actual_of(transfers))
report = sealed["report"]
print("   HTTP", status, "matched:", report["matched"])
print("   summary:", json.dumps(report["summary"]))
print("   stock summary:", json.dumps(report["stock_summary"]))

print("3) same review id + same content replays the sealed report (200):")
status, again = review(rid, "review-py-001", actual_of(transfers))
print("   HTTP", status, "identical:", again == sealed)

print("4) same review id + different content -> 409 conflict:")
changed = actual_of(transfers)
changed[0]["volume_nl"] -= 100
status, body = review(rid, "review-py-001", changed)
print("   HTTP", status, body["error"])

print("5) the sealed report is fetchable and immutable:")
status, fetched = call(f"/reviews/{sealed['review_id']}")
print("   GET -> HTTP", status, "same as sealed:", fetched == sealed)

print("6) abort a second plan mid-way; the review reports the missing steps:")
rid2 = "review-py-2"
status, doc = call("/inventory/plans", PLAN_BODY, request_id=rid2)
transfers2 = doc["plan"]["transfers"]
register(rid2, transfers2[0])  # only step 1 (a 600 nL stock-out) really ran
status, body = call(f"/inventory/plans/{rid2}/abort", body={})
print("   abort -> HTTP", status, "released:", json.dumps(body["released"]))
status, sealed2 = review(rid2, "review-py-002", actual_of(transfers2[:1]))
report2 = sealed2["report"]
print("   review -> HTTP", status, "matched:", report2["matched"])
print("   first missing step:", report2["first_missing_step"],
      "| missing:", report2["summary"]["missing_steps"], "of",
      report2["summary"]["total_steps"])
print("   stock summary:", json.dumps(report2["stock_summary"]))

print("7) a doctored record shows per-step mismatches and extra records:")
rid3 = "review-py-3"
status, doc = call("/inventory/plans", PLAN_BODY, request_id=rid3)
transfers3 = doc["plan"]["transfers"]
for t in transfers3:
    register(rid3, t)
call(f"/inventory/plans/{rid3}/complete", body={})
doctored = actual_of(transfers3)
doctored[0]["volume_nl"] -= 100                      # wrong volume on step 1
doctored.append({"step": len(transfers3) + 1,        # an extra transfer
                 "source": {"kind": "stock", "id": "S1"},
                 "destination": {"kind": "well", "id": "A1"},
                 "volume_nl": 500})
status, sealed3 = review(rid3, "review-py-003", doctored)
report3 = sealed3["report"]
print("   HTTP", status, "matched:", report3["matched"])
step1 = report3["steps"][0]
print("   step 1:", step1["status"], "differences:", step1["differences"])
print("   first mismatch:", report3["first_mismatch_step"],
      "| first extra:", report3["first_extra_step"])
print("   stock summary:", json.dumps(report3["stock_summary"]))

print("8) rejections seal nothing and leave the review id reusable:")
status, body = review("no-such-plan", "review-py-004", actual_of(transfers))
print("   unknown plan -> HTTP", status, body["error"])
rid4 = "review-py-4"
status, doc = call("/inventory/plans", PLAN_BODY, request_id=rid4)
status, body = review(rid4, "review-py-004", actual_of(transfers))
print("   unended plan -> HTTP", status, body["error"],
      "(status:", str(body["status"]) + ")")
dup = actual_of(transfers) + [actual_of(transfers)[0]]
status, body = review(rid4, "review-py-004", dup)
print("   duplicate steps -> HTTP", status, body["error"],
      "steps:", body["steps"])
status, body = call("/reviews/review-py-004")
print("   nothing sealed -> GET /reviews/review-py-004 HTTP", status)
for t in transfers:
    register(rid4, t)
call(f"/inventory/plans/{rid4}/complete", body={})
status, body = review(rid4, "review-py-004", actual_of(transfers))
print("   after completion the same review id seals -> HTTP", status,
      "matched:", body["report"]["matched"])
