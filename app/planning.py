"""Convert validated HTTP models into the solver Problem and its answer back."""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Optional

from .models import InventoryPlanIn, PlanRequest
from .solver import Problem, Well


def _fraction_str(f: Fraction) -> str:
    return str(f)


def _problem(req, stocks: dict) -> Problem:
    """Shared request -> Problem mapping for both planning modes."""
    wells = []
    for w in req.wells:
        cap = w.capacity_nl
        if cap is None:
            cap = req.default_well_capacity_nl
        wells.append(Well(
            id=w.id,
            volume=w.final_volume_nl,
            conc=Fraction(w.final_concentration),
            capacity=cap,
        ))
    containers = [c.capacity_nl for c in req.containers]
    return Problem(
        stocks=stocks,
        diluent_volume=req.diluent.available_nl,
        wells=wells,
        containers=containers,
        pip_min=req.pipette.min_nl,
        pip_max=req.pipette.max_nl,
        pip_step=req.pipette.step_nl,
        max_depth=req.limits.max_depth,
        max_search_nodes=req.limits.max_search_nodes,
        max_enumeration_units=req.limits.max_enumeration_units,
        max_factor=req.limits.max_factor,
    )


def build_problem(req: PlanRequest) -> Problem:
    stocks = {
        s.id: {"conc": Fraction(s.concentration), "volume": s.available_nl}
        for s in req.stocks
    }
    return _problem(req, stocks)


def build_inventory_problem(req: InventoryPlanIn, batches: dict) -> Problem:
    """Problem whose stock volumes are the batches' *available* volumes."""
    stocks = {
        bid: {
            "conc": Fraction(batches[bid]["concentration"]),
            "volume": batches[bid]["available_nl"],
        }
        for bid in req.batch_ids
    }
    return _problem(req, stocks)


def stock_usage_from_plan(plan: dict) -> dict:
    """Total nanoliters each stock (batch) actually loses in the plan."""
    usage: dict[str, int] = {}
    for t in plan["transfers"]:
        src = t["source"]
        if src.get("kind") == "stock":
            usage[src["id"]] = usage.get(src["id"], 0) + t["volume_nl"]
    return usage


def _node_key(node: dict) -> tuple:
    """Identity key for a transfer endpoint.

    Intermediates are identified by their container (the physical vessel the
    intermediate liquid resides in); stocks and wells by their id.
    """
    kind = node.get("kind")
    if kind == "intermediate":
        return ("intermediate", node.get("container"))
    if kind == "stock":
        return ("stock", node.get("id"))
    if kind == "well":
        return ("well", node.get("id"))
    return (kind, node.get("id"))


def traced_wells_for_batch(plan: dict, batch_id: str) -> list[str]:
    """Wells that receive liquid originating from ``batch_id``.

    The trace follows transfer steps forward from the batch stock through any
    intermediate vessels to the target wells, so wells reached only through a
    serial-dilution chain are included. Each well is reported at most once,
    in first-reach (transfer-step) order.
    """
    reached = {("stock", batch_id)}
    wells: list[str] = []
    seen = set()
    for t in plan["transfers"]:
        src = _node_key(t["source"])
        dst = t["destination"]
        if src not in reached:
            continue
        dkey = _node_key(dst)
        if dkey in reached:
            continue
        reached.add(dkey)
        if dst.get("kind") == "well" and dst.get("id") not in seen:
            seen.add(dst["id"])
            wells.append(dst["id"])
    return wells


def container_ids(req) -> list[str]:
    ids = []
    for i, c in enumerate(req.containers):
        ids.append(c.id if c.id is not None else f"C{i}")
    return ids


def relabel_plan(plan: dict, cids: list[str]) -> dict:
    """Map solver's C0/C1... container labels to user container ids."""
    for im in plan["intermediates"]:
        idx = int(im["container"][1:])
        im["container"] = cids[idx]
    for t in plan["transfers"]:
        for side in ("source", "destination"):
            lab = t[side]
            if lab.get("kind") == "intermediate" and lab.get("container"):
                idx = int(lab["container"][1:])
                lab["container"] = cids[idx]
    return plan


def _validate_common(req) -> None:
    well_ids = [w.id for w in req.wells]
    if len(set(well_ids)) != len(well_ids):
        raise ValueError("duplicate well id")
    cids = [c.id for c in req.containers if c.id is not None]
    if len(cids) != len(set(cids)):
        raise ValueError("duplicate container id")
    p = req.pipette
    if p.min_nl > p.max_nl:
        raise ValueError("pipette.min_nl must be <= pipette.max_nl")
    if p.step_nl > (p.max_nl - 0):
        raise ValueError("pipette.step_nl must be <= pipette.max_nl")
    # there must be a step-aligned volume inside the range
    k0 = -((-p.min_nl) // p.step_nl)
    k1 = p.max_nl // p.step_nl
    if k0 > k1:
        raise ValueError(
            "no step-aligned transfer volume exists within pipette range")


def cross_validate_references(req: PlanRequest) -> None:
    """Semantic validation that needs the whole request."""
    stock_ids = [s.id for s in req.stocks]
    if len(set(stock_ids)) != len(stock_ids):
        raise ValueError("duplicate stock id")
    _validate_common(req)


def cross_validate_inventory(req: InventoryPlanIn) -> None:
    """Semantic validation for inventory-mode plan requests."""
    if len(set(req.batch_ids)) != len(req.batch_ids):
        raise ValueError("duplicate batch id")
    _validate_common(req)


def canonical_request(req) -> bytes:
    """Stable canonical serialization used for idempotency conflict checks."""
    data = req.model_dump()
    # The stepwise flag is an *optional* extension: normalize the default
    # value away so requests created before the feature existed keep the
    # same frozen-hash replay semantics.
    if data.get("execution_mode") == "whole":
        data.pop("execution_mode", None)

    def norm(x):
        if isinstance(x, Fraction):
            return {"__fraction__": str(x)}
        if isinstance(x, dict):
            return {k: norm(x[k]) for k in sorted(x)}
        if isinstance(x, list):
            return [norm(v) for v in x]
        return x

    return json.dumps(norm(data), sort_keys=True, separators=(",", ":")).encode()


def request_hash(req) -> str:
    return hashlib.sha256(canonical_request(req)).hexdigest()


# --------------------------------------------------------------------------- #
# Stepwise execution
# --------------------------------------------------------------------------- #


def frozen_transfer(plan: dict, step: int) -> Optional[dict]:
    """The frozen transfer with the given step number, or ``None``."""
    for t in plan.get("transfers", []):
        if t["step"] == step:
            return t
    return None


def transfers_match(frozen_transfer: dict, source: dict, destination: dict,
                    volume_nl: int) -> bool:
    """Whether a registered step is exactly the frozen step.

    The registration must reproduce source, destination and volume verbatim
    (same node kind/id/container and the same integer volume); a different
    content is rejected even for the same step number.
    """
    if frozen_transfer["volume_nl"] != volume_nl:
        return False
    for label, registered in (("source", source), ("destination", destination)):
        frozen_node = frozen_transfer[label]
        if frozen_node.get("kind") != registered.get("kind"):
            return False
        if frozen_node.get("id") != registered.get("id"):
            return False
        if frozen_node.get("container") != registered.get("container"):
            return False
    return True


def registered_stock_usage(steps: list[dict]) -> dict:
    """Per-batch nanoliters *already drawn* by the registered step prefix.

    Intermediate/diluent steps never move stock inventory: only a step whose
    source is a stock batch counts, and each step is counted once (the steps
    are a valid registration prefix -- no gaps, unique step numbers).
    """
    used: dict[str, int] = {}
    seen = set()
    for reg in steps:
        n = reg["step"]
        if n in seen:
            continue
        seen.add(n)
        src = reg["source"]
        if src.get("kind") == "stock" and src.get("id"):
            used[src["id"]] = used.get(src["id"], 0) + reg["volume_nl"]
    return used


def stepwise_progress(frozen_plan: dict, registered_steps: list[dict],
                      reservation_items: list[dict]) -> dict:
    """Progress + per-batch used/pending bookkeeping for a stepwise plan.

    ``used_nl`` is the volume whose stock-out step has already been
    registered (and has therefore moved from reserved to used);
    ``pending_nl`` is the frozen reservation not yet drawn and still
    releasable on abort.
    """
    total_steps = len(frozen_plan.get("transfers", []))
    done = sorted(s["step"] for s in registered_steps)
    drawn = registered_stock_usage(registered_steps)
    batch_progress = []
    for item in reservation_items:
        bid = item["batch_id"]
        used = drawn.get(bid, 0)
        batch_progress.append({
            "batch_id": bid,
            "reserved_total_nl": item["amount_nl"],
            "used_nl": used,
            "pending_nl": item["amount_nl"] - used,
        })
    return {
        "total_steps": total_steps,
        "registered_steps": done,
        "registered_count": len(done),
        "next_step": done[-1] + 1 if done else 1,
        "complete": len(done) == total_steps and total_steps > 0,
        "batch_progress": batch_progress,
    }


def executed_prefix_plan(plan: dict, registered_steps: list[int]) -> dict:
    """The frozen plan restricted to its *executed* step prefix.

    Recall impact for a partially executed stepwise plan is adjudicated only
    along the transfers already registered: stock reserved for not-yet-taken
    steps has never left the batch and must not appear in the report.
    """
    done = set(registered_steps)
    out = dict(plan)
    out["transfers"] = [t for t in plan.get("transfers", [])
                        if t["step"] in done]
    return out


def traced_wells_for_executed_steps(plan: dict, batch_id: str,
                                    registered_steps: list[int]) -> list[str]:
    """Wells reached from ``batch_id`` through the executed step prefix only."""
    return traced_wells_for_batch(
        executed_prefix_plan(plan, registered_steps), batch_id)


# --------------------------------------------------------------------------- #
# Execution-variance review
# --------------------------------------------------------------------------- #


def _canonical_node(node: dict) -> dict:
    return {"kind": node.get("kind"), "id": node.get("id"),
            "container": node.get("container")}


def review_hash(request_id: str, actual_transfers: list[dict]) -> str:
    """Stable content hash for review idempotency / conflict detection.

    Records are normalized -- sorted by step number, nodes reduced to
    kind/id/container -- so a retry carrying the same records in a different
    submission order still hashes identically, while any content change
    (different plan, step, node or volume) hashes differently.
    """
    norm = [
        {"step": t["step"],
         "source": _canonical_node(t["source"]),
         "destination": _canonical_node(t["destination"]),
         "volume_nl": t["volume_nl"]}
        for t in sorted(actual_transfers, key=lambda x: x["step"])
    ]
    data = {"request_id": request_id, "actual_transfers": norm}
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _nodes_differ(frozen_node: dict, actual_node: dict) -> bool:
    return any(frozen_node.get(k) != actual_node.get(k)
               for k in ("kind", "id", "container"))


def compare_actual_transfers(plan: dict, actual_transfers: list[dict]) -> dict:
    """Step-by-step comparison of the actual record against the frozen plan.

    ``actual_transfers`` is the submitter's full ordered record
    (``{step, source, destination, volume_nl}`` dicts with unique step
    numbers -- duplicates are rejected by the caller). The comparison is
    anchored on the frozen plan: every frozen step is

    * ``matched``           - an actual record with the same step number
                              reproduces source/destination/volume verbatim;
    * ``content_mismatch``  - same step number but different content (the
                              differing fields are named in ``differences``);
    * ``missing``           - no actual record carries this step number.

    Actual records whose step number no frozen step owns are ``extra``.
    The result also carries the first position of each anomaly class and the
    frozen-vs-actual stock-out summary per batch (frozen = volume the plan
    draws from each batch; actual = volume the submitted record draws).
    Pure function: the review never mutates inventory, plans or registered
    steps.
    """
    frozen_steps = plan.get("transfers", [])
    actual_by_step = {t["step"]: t for t in actual_transfers}
    frozen_numbers = {f["step"] for f in frozen_steps}

    steps_out = []
    matched = missing = mismatched = 0
    first_missing = first_mismatch = None
    for f in frozen_steps:
        n = f["step"]
        a = actual_by_step.get(n)
        if a is None:
            missing += 1
            if first_missing is None:
                first_missing = n
            steps_out.append({"step": n, "status": "missing",
                              "frozen": f, "actual": None})
            continue
        diffs = []
        if _nodes_differ(f["source"], a["source"]):
            diffs.append("source")
        if _nodes_differ(f["destination"], a["destination"]):
            diffs.append("destination")
        if f["volume_nl"] != a["volume_nl"]:
            diffs.append("volume_nl")
        if diffs:
            mismatched += 1
            if first_mismatch is None:
                first_mismatch = n
            steps_out.append({"step": n, "status": "content_mismatch",
                              "frozen": f, "actual": a,
                              "differences": diffs})
        else:
            matched += 1
            steps_out.append({"step": n, "status": "matched",
                              "frozen": f, "actual": a})

    extras = sorted((t for s, t in actual_by_step.items()
                     if s not in frozen_numbers),
                    key=lambda t: t["step"])

    frozen_usage = stock_usage_from_plan(plan)
    actual_usage: dict[str, int] = {}
    for t in actual_transfers:
        src = t["source"]
        if src.get("kind") == "stock" and src.get("id"):
            actual_usage[src["id"]] = (actual_usage.get(src["id"], 0)
                                       + t["volume_nl"])
    stock_summary = [
        {"batch_id": b,
         "frozen_nl": frozen_usage.get(b, 0),
         "actual_nl": actual_usage.get(b, 0),
         "difference_nl": actual_usage.get(b, 0) - frozen_usage.get(b, 0)}
        for b in sorted(set(frozen_usage) | set(actual_usage))
    ]

    return {
        "matched": not (missing or mismatched or extras),
        "summary": {
            "total_steps": len(frozen_steps),
            "actual_records": len(actual_transfers),
            "matched_steps": matched,
            "missing_steps": missing,
            "mismatched_steps": mismatched,
            "extra_records": len(extras),
        },
        "steps": steps_out,
        "extra_records": extras,
        "first_missing_step": first_missing,
        "first_mismatch_step": first_mismatch,
        "first_extra_step": extras[0]["step"] if extras else None,
        "stock_summary": stock_summary,
    }
