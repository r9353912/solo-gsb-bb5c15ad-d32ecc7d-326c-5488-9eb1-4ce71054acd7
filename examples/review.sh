#!/usr/bin/env bash
# Execution-variance review demo against a locally running service
# (docker compose up).
#
# Runs a stepwise plan to completion, then reviews it: the full ordered
# actual transfer record is posted under a unique review id and compared
# step by step against the frozen plan. Demonstrates:
#   * 201 seals an immutable report (per-step results, first missing /
#     extra / mismatch position, frozen-vs-actual stock-out summary);
#   * same review id + same content -> 200 replay; different content -> 409;
#   * the report never changes inventory, plans or registered steps;
#   * unknown / unended plans, duplicate steps, illegal nodes and
#     non-positive volumes reject the whole submission and seal nothing;
#   * GET /reviews/{review_id} fetches the sealed report.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
RID="review-demo-001"
RID2="review-demo-002"

post() { curl -sS -X POST "$BASE$1" "${@:2}"; }

echo "== register batch S1 and complete a stepwise plan =="
post /batches -H "Content-Type: application/json" \
  -d '{"id":"S1","concentration":"1000","initial_volume_nl":1000000}' \
  | python3 -m json.tool
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID" \
  --data @examples/inventory_stepwise.json > /dev/null

# register every frozen step, then complete the plan
python3 - "$BASE" "$RID" <<'PY'
import json, sys, urllib.request

BASE, RID = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{BASE}/inventory/plans/{RID}") as resp:
    transfers = json.loads(resp.read())["plan"]["transfers"]
for t in transfers:
    req = urllib.request.Request(
        f"{BASE}/inventory/plans/{RID}/steps",
        data=json.dumps({"step": t["step"], "source": t["source"],
                         "destination": t["destination"],
                         "volume_nl": t["volume_nl"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req)
req = urllib.request.Request(f"{BASE}/inventory/plans/{RID}/complete",
                             data=b"{}", method="POST",
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req)
print(f"   plan {RID} completed ({len(transfers)} steps)")
PY

echo "== batch balances BEFORE the review (review must not change them) =="
curl -sS "$BASE/batches/S1" | python3 -m json.tool

echo "== review the completed plan with the exact actual record -> 201 =="
# build actual_transfers from the frozen plan and POST the review
python3 - "$BASE" "$RID" <<'PY'
import json, sys, urllib.error, urllib.request

BASE, RID = sys.argv[1], sys.argv[2]


def call(path, body=None, review_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if review_id:
        headers["X-Review-ID"] = review_id
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


_, doc = call(f"/inventory/plans/{RID}")
transfers = doc["plan"]["transfers"]
actual = [{"step": t["step"], "source": t["source"],
           "destination": t["destination"], "volume_nl": t["volume_nl"]}
          for t in transfers]

code, sealed = call(f"/inventory/plans/{RID}/review",
                    {"actual_transfers": actual}, review_id="review-001")
report = sealed["report"]
print("   HTTP", code, "matched:", report["matched"])
print("   summary:", json.dumps(report["summary"]))
print("   stock summary:", json.dumps(report["stock_summary"]))

print("== same review id + same content -> 200 replay (identical body) ==")
code, again = call(f"/inventory/plans/{RID}/review",
                   {"actual_transfers": actual}, review_id="review-001")
print("   HTTP", code, "identical:", again == sealed)

print("== same review id + different content -> 409 idempotency_conflict ==")
changed = [dict(a) for a in actual]
changed[0] = dict(changed[0], volume_nl=changed[0]["volume_nl"] - 100)
code, body = call(f"/inventory/plans/{RID}/review",
                  {"actual_transfers": changed}, review_id="review-001")
print("   HTTP", code, body["error"])

print("== GET /reviews/review-001 returns the sealed report ==")
code, fetched = call("/reviews/review-001")
print("   HTTP", code, "same as sealed:", fetched == sealed)

print("== divergent record under a fresh review id: mismatch + extra ==")
doctored = [dict(a) for a in actual]
doctored[0] = dict(doctored[0],
                   volume_nl=doctored[0]["volume_nl"] - 100)
doctored.append({"step": len(transfers) + 1,
                 "source": {"kind": "stock", "id": "S1"},
                 "destination": {"kind": "well", "id": "A1"},
                 "volume_nl": 500})
code, sealed2 = call(f"/inventory/plans/{RID}/review",
                     {"actual_transfers": doctored},
                     review_id="review-002")
r2 = sealed2["report"]
print("   HTTP", code, "matched:", r2["matched"])
print("   step 1:", r2["steps"][0]["status"],
      "differences:", r2["steps"][0]["differences"])
print("   first mismatch:", r2["first_mismatch_step"],
      "| first extra:", r2["first_extra_step"])
print("   stock summary:", json.dumps(r2["stock_summary"]))

print("== rejections seal nothing (review id stays reusable) ==")
code, body = call("/inventory/plans/no-such-plan/review",
                  {"actual_transfers": actual}, review_id="review-003")
print("   unknown plan -> HTTP", code, body["error"])
dup = actual + [actual[0]]
code, body = call(f"/inventory/plans/{RID}/review",
                  {"actual_transfers": dup}, review_id="review-003")
print("   duplicate steps -> HTTP", code, body["error"], body["steps"])
code, body = call("/reviews/review-003")
print("   GET /reviews/review-003 -> HTTP", code, "(nothing sealed)")
PY

echo "== aborted plan: review reports the missing steps =="
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID2" \
  --data @examples/inventory_stepwise.json > /dev/null
python3 - "$BASE" "$RID2" <<'PY'
import json, sys, urllib.error, urllib.request

BASE, RID = sys.argv[1], sys.argv[2]


def call(path, body=None, review_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if review_id:
        headers["X-Review-ID"] = review_id
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


_, doc = call(f"/inventory/plans/{RID}")
transfers = doc["plan"]["transfers"]
first = transfers[0]
call(f"/inventory/plans/{RID}/steps", {
    "step": first["step"], "source": first["source"],
    "destination": first["destination"], "volume_nl": first["volume_nl"]})
code, body = call(f"/inventory/plans/{RID}/abort", {}, method="POST")
print("   abort -> HTTP", code, "released:", json.dumps(body["released"]))
actual = [{"step": first["step"], "source": first["source"],
           "destination": first["destination"],
           "volume_nl": first["volume_nl"]}]
code, sealed = call(f"/inventory/plans/{RID}/review",
                    {"actual_transfers": actual}, review_id="review-004")
report = sealed["report"]
print("   review -> HTTP", code, "matched:", report["matched"])
print("   first missing step:", report["first_missing_step"],
      "| missing:", report["summary"]["missing_steps"], "of",
      report["summary"]["total_steps"])
print("   stock summary:", json.dumps(report["stock_summary"]))
PY

echo "== batch balances AFTER the reviews (unchanged) =="
curl -sS "$BASE/batches/S1" | python3 -m json.tool
