#!/usr/bin/env bash
# Execution-variance reconciliation review demo against a locally running
# service (docker compose up).
#
# Creates stepwise inventory plans, finishes them (complete / abort), then
# submits the full ordered actual transfer record for reconciliation.
# Demonstrates:
#   * per-step matched / missing / content_mismatch results, the first
#     discrepancy and frozen vs actual per-batch stock-out totals;
#   * same X-Review-ID + same content replays the sealed report (200),
#     different content conflicts (409);
#   * unknown plan, unfinished plan, duplicate steps, illegal nodes and
#     non-positive volumes are rejected whole -- no report is sealed;
#   * a review never changes inventory, the plan or registered steps.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
post() { curl -sS -X POST "$BASE$1" "${@:2}"; }

echo "== register batch S1 (1000 uM, 1 mL) =="
post /batches -H "Content-Type: application/json" \
  -d '{"id":"S1","concentration":"1000","initial_volume_nl":1000000}' \
  | python3 -m json.tool

echo
echo "== plan A: execute every step, then complete =="
RID_A="review-demo-A"
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID_A" \
  --data @examples/inventory_stepwise.json > /dev/null
python3 - "$BASE" "$RID_A" <<'PY'
import json, sys, urllib.error, urllib.request

BASE, RID = sys.argv[1], sys.argv[2]


def call(path, body=None, review_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if review_id:
        headers["X-Review-ID"] = review_id
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


_, doc = call(f"/inventory/plans/{RID}")
transfers = doc["plan"]["transfers"]
for t in transfers:
    call(f"/inventory/plans/{RID}/steps", {
        "step": t["step"], "source": t["source"],
        "destination": t["destination"], "volume_nl": t["volume_nl"]})
call(f"/inventory/plans/{RID}/complete", {})

def record(ts):
    return {"actual_transfers": [
        {"step": t["step"], "source": t["source"],
         "destination": t["destination"], "volume_nl": t["volume_nl"]}
        for t in ts]}

print("== review with the exact executed record -> 201, all_match ==")
code, report = call(f"/inventory/plans/{RID}/reviews", record(transfers),
                    review_id="review-sh-A")
print("   HTTP", code, "all_match:", report["all_match"],
      "summary:", report["summary"])
print("   stock out:", json.dumps(report["stock_out_summary"]["batches"]))

print("== same X-Review-ID + same content -> 200 replay ==")
code2, again = call(f"/inventory/plans/{RID}/reviews", record(transfers),
                    review_id="review-sh-A")
print("   HTTP", code2, "identical:", again == report)

print("== same X-Review-ID, wrong volume on step 1 -> 409 conflict ==")
changed = json.loads(json.dumps(transfers))
changed[0]["volume_nl"] -= 100
code, body = call(f"/inventory/plans/{RID}/reviews", record(changed),
                  review_id="review-sh-A")
print("   HTTP", code, body["error"])

print("== fresh review id, wrong volume -> first discrepancy content_mismatch ==")
code, report = call(f"/inventory/plans/{RID}/reviews", record(changed),
                    review_id="review-sh-A-wrong")
fd = report["first_discrepancy"]
print("   HTTP", code, "first:", fd["step"], fd["status"],
      "differences:", fd["differences"])
print("   frozen vs actual:",
      json.dumps(report["stock_out_summary"]["batches"]))
PY

echo
echo "== plan B: register step 1 only, then abort; review shows missing =="
RID_B="review-demo-B"
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID_B" \
  --data @examples/inventory_stepwise.json > /dev/null
python3 - "$BASE" "$RID_B" <<'PY'
import json, sys, urllib.error, urllib.request

BASE, RID = sys.argv[1], sys.argv[2]


def call(path, body=None, review_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if review_id:
        headers["X-Review-ID"] = review_id
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


_, doc = call(f"/inventory/plans/{RID}")
first = doc["plan"]["transfers"][0]
call(f"/inventory/plans/{RID}/steps", {
    "step": first["step"], "source": first["source"],
    "destination": first["destination"], "volume_nl": first["volume_nl"]})
call(f"/inventory/plans/{RID}/abort", {})
record = {"actual_transfers": [{
    "step": first["step"], "source": first["source"],
    "destination": first["destination"], "volume_nl": first["volume_nl"]}]}
code, report = call(f"/inventory/plans/{RID}/reviews", record,
                    review_id="review-sh-B")
print("   HTTP", code, "status:", report["execution_status"])
print("   missing steps:", report["missing_steps"])
print("   first discrepancy:", report["first_discrepancy"]["step"],
      report["first_discrepancy"]["status"])
print("   stock out frozen/actual/delta:",
      report["stock_out_summary"]["frozen_total_nl"],
      report["stock_out_summary"]["actual_total_nl"],
      report["stock_out_summary"]["delta_nl"])
PY

echo
echo "== whole-request rejections (no report is sealed) =="
RID_C="review-demo-C"
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID_C" \
  --data @examples/inventory_stepwise.json > /dev/null
echo "-- still-running plan -> 409 plan_not_finished:"
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  "$BASE/inventory/plans/$RID_C/reviews" \
  -H "Content-Type: application/json" -H "X-Review-ID: review-sh-running" \
  -d '{"actual_transfers":[]}'
echo "-- duplicate step numbers -> 400 duplicate_step:"
curl -sS -X POST "$BASE/inventory/plans/$RID_B/reviews" \
  -H "Content-Type: application/json" -H "X-Review-ID: review-sh-dup" \
  -d '{"actual_transfers":[
        {"step":1,"source":{"kind":"stock","id":"S1"},
         "destination":{"kind":"intermediate","id":"I1","container":"tube-2"},
         "volume_nl":600},
        {"step":1,"source":{"kind":"stock","id":"S1"},
         "destination":{"kind":"intermediate","id":"I1","container":"tube-2"},
         "volume_nl":600}]}'
echo
echo "-- illegal node (well used as source) -> 400 illegal_node:"
curl -sS -X POST "$BASE/inventory/plans/$RID_B/reviews" \
  -H "Content-Type: application/json" -H "X-Review-ID: review-sh-illegal" \
  -d '{"actual_transfers":[
        {"step":1,"source":{"kind":"well","id":"A1"},
         "destination":{"kind":"intermediate","id":"I1","container":"tube-2"},
         "volume_nl":600}]}'
echo
echo "-- non-positive volume -> 400 (pydantic validation):"
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  "$BASE/inventory/plans/$RID_B/reviews" \
  -H "Content-Type: application/json" -H "X-Review-ID: review-sh-zero" \
  -d '{"actual_transfers":[
        {"step":1,"source":{"kind":"stock","id":"S1"},
         "destination":{"kind":"intermediate","id":"I1","container":"tube-2"},
         "volume_nl":0}]}'
echo "-- unknown plan -> 404 unknown_plan:"
curl -sS -X POST "$BASE/inventory/plans/no-such-plan/reviews" \
  -H "Content-Type: application/json" -H "X-Review-ID: review-sh-unknown" \
  -d '{"actual_transfers":[]}'
echo
echo "-- none of the rejected requests sealed a report:"
for rid in review-sh-running review-sh-dup review-sh-illegal review-sh-zero \
           review-sh-unknown; do
  printf "   %s -> " "$rid"
  curl -sS -o /dev/null -w "%{http_code}\n" "$BASE/reviews/$rid"
done

echo
echo "== sealed reports are retrievable =="
curl -sS "$BASE/reviews/review-sh-A" | python3 -m json.tool
