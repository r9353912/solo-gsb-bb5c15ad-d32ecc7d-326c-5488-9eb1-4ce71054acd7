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
# Post-execution reconciliation reviews
# --------------------------------------------------------------------------- #


def _node_view(node: dict) -> dict:
    """Canonical node shape (kind/id/container, missing keys -> ``None``)."""
    return {"kind": node.get("kind"),
            "id": node.get("id"),
            "container": node.get("container")}


def _transfer_view(t: dict) -> dict:
    """Canonical transfer shape as frozen/compared in a review."""
    return {"step": t["step"],
            "source": _node_view(t["source"]),
            "destination": _node_view(t["destination"]),
            "volume_nl": t["volume_nl"]}


def nodes_equal(left: dict, right: dict) -> bool:
    """Exact node equality on kind/id/container."""
    return _node_view(left) == _node_view(right)


def validate_actual_transfers(transfers: list[dict]):
    """Structural validation of a submitted actual-transfer record list.

    These conditions reject the *whole* review request (400) without sealing
    any report:

    * a step number occurs more than once (``duplicate_step``);
    * the records are not strictly ascending by step number
      (``steps_not_ordered`` -- the contract requires the full ordered
      record; gaps are allowed and show up as *missing* steps in the report);
    * a node is structurally illegal (``illegal_node``): a well used as a
      source, a stock/diluent used as a destination, a stock node without an
      id, or an intermediate node without a container.

    Semantic differences against the frozen plan (unknown batches, other
    sources/destinations, other volumes) are *not* illegal -- they are the
    discrepancies the review exists to report. Returns ``None`` when valid,
    otherwise an error descriptor.
    """
    seen: set[int] = set()
    previous = 0
    for t in transfers:
        n = t["step"]
        if n in seen:
            return {"error": "duplicate_step",
                    "detail": f"step {n} appears more than once in the "
                              "actual transfer record",
                    "step": n}
        if n <= previous:
            return {"error": "steps_not_ordered",
                    "detail": "actual transfers must be strictly ordered by "
                              f"step number; step {n} follows step {previous}",
                    "step": n, "previous_step": previous}
        seen.add(n)
        previous = n
        for role in ("source", "destination"):
            node = _node_view(t[role])
            kind = node["kind"]
            illegal = None
            if role == "source":
                if kind == "well":
                    illegal = "a well can only be a transfer destination"
                elif kind == "stock" and not node["id"]:
                    illegal = "a stock source node requires an id"
                elif kind == "intermediate" and not node["container"]:
                    illegal = "an intermediate source node requires a container"
            else:
                if kind in ("stock", "diluent"):
                    illegal = f"a {kind} node can only be a transfer source"
                elif kind == "well" and not node["id"]:
                    illegal = "a well destination node requires an id"
                elif kind == "intermediate" and not node["container"]:
                    illegal = ("an intermediate destination node requires "
                               "a container")
            if illegal is not None:
                return {"error": "illegal_node",
                        "detail": f"illegal {role} node on step {n}: {illegal}",
                        "step": n, "role": role, "node": node}
    return None


def _stock_totals(transfers: list[dict]):
    """Per-batch stock-out totals and the step numbers that produce them."""
    totals: dict[str, int] = {}
    steps: dict[str, list[int]] = {}
    for t in sorted(transfers, key=lambda x: x["step"]):
        src = t["source"]
        if src["kind"] != "stock":
            continue
        bid = src["id"]
        totals[bid] = totals.get(bid, 0) + t["volume_nl"]
        steps.setdefault(bid, []).append(t["step"])
    return totals, steps


def build_execution_review(plan: dict, actual_transfers: list[dict],
                           plan_status: str) -> dict:
    """Compare the frozen plan step by step with the actual transfer record.

    Produces an immutable review body:

    * ``steps[]`` -- one entry per step number in the union of frozen and
      actual step numbers, each marked ``matched`` / ``missing`` (frozen but
      not executed) / ``extra`` (executed but not frozen) /
      ``content_mismatch`` (same step number, different source/destination/
      volume), with the frozen and/or actual transfer attached;
    * ``first_discrepancy`` -- the first entry (ascending step number) that is
      not a match, or ``null`` when everything matches;
    * ``stock_out_summary`` -- frozen vs actual nanoliters actually drawn from
      each stock batch, per-batch step lists and actual-minus-frozen deltas.

    The function is pure: it never mutates the plan, the steps or any
    inventory state.
    """
    frozen = {t["step"]: _transfer_view(t)
              for t in plan.get("transfers", [])}
    actual = {t["step"]: _transfer_view(t) for t in actual_transfers}

    entries = []
    counts = {"matched": 0, "missing": 0, "extra": 0,
              "content_mismatch": 0}
    first_discrepancy = None
    for n in sorted(frozen.keys() | actual.keys()):
        f = frozen.get(n)
        a = actual.get(n)
        entry: dict = {"step": n}
        if f is not None and a is None:
            kind = "missing"
        elif a is not None and f is None:
            kind = "extra"
        elif not transfers_match(f, a["source"], a["destination"],
                                 a["volume_nl"]):
            kind = "content_mismatch"
            diffs = []
            if not nodes_equal(f["source"], a["source"]):
                diffs.append("source")
            if not nodes_equal(f["destination"], a["destination"]):
                diffs.append("destination")
            if f["volume_nl"] != a["volume_nl"]:
                diffs.append("volume_nl")
            entry["differences"] = diffs
        else:
            kind = "matched"
        entry["status"] = kind
        if f is not None:
            entry["frozen"] = f
        if a is not None:
            entry["actual"] = a
        counts[kind] += 1
        entries.append(entry)
        if kind != "matched" and first_discrepancy is None:
            first_discrepancy = dict(entry)

    frozen_totals, frozen_steps = _stock_totals(list(frozen.values()))
    actual_totals, actual_steps = _stock_totals(list(actual.values()))
    batches = []
    for bid in sorted(frozen_totals.keys() | actual_totals.keys()):
        f_nl = frozen_totals.get(bid, 0)
        a_nl = actual_totals.get(bid, 0)
        batches.append({
            "batch_id": bid,
            "frozen_nl": f_nl,
            "actual_nl": a_nl,
            "delta_nl": a_nl - f_nl,
            "frozen_steps": frozen_steps.get(bid, []),
            "actual_steps": actual_steps.get(bid, []),
        })
    frozen_total = sum(frozen_totals.values())
    actual_total = sum(actual_totals.values())

    return {
        "execution_status": plan_status,
        "execution_mode": "stepwise",
        "frozen_step_count": len(frozen),
        "actual_step_count": len(actual),
        "all_match": (counts["matched"] == len(frozen) == len(actual)),
        "summary": {
            "total": len(entries),
            **counts,
        },
        "missing_steps": [e["step"] for e in entries
                          if e["status"] == "missing"],
        "extra_steps": [e["step"] for e in entries
                        if e["status"] == "extra"],
        "mismatch_steps": [e["step"] for e in entries
                           if e["status"] == "content_mismatch"],
        "first_discrepancy": first_discrepancy,
        "steps": entries,
        "stock_out_summary": {
            "frozen_total_nl": frozen_total,
            "actual_total_nl": actual_total,
            "delta_nl": actual_total - frozen_total,
            "batches": batches,
        },
    }


def review_content_hash(request_id: str, transfers: list[dict]) -> str:
    """Stable hash of a review submission (plan id + ordered actual record).

    Same review id + same content replays the original sealed report; same
    review id with different content is a conflict.
    """
    payload = {"request_id": request_id,
               "transfers": [_transfer_view(t) for t in transfers]}
    blob = json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
