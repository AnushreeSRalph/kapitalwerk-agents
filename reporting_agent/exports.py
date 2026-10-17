"""Minimal read/export layer over bucket_output.

This is the seam a future UI or HTTP API will sit on top of. It deliberately
"""


from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime

import case_actions
import notifications
import top_risk

# governance's audit store lives one folder over, needed only for audit-summary.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "governance"))

DEFAULT_AUDIT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "bucket_output", "_audit", "aml_audit.jsonl"
)


def _is_gcs_path(path: str) -> bool:
    return isinstance(path, str) and path.startswith("gs://")


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    without_scheme = uri[len("gs://"):]
    if "/" not in without_scheme:
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    bucket, blob = without_scheme.split("/", 1)
    return bucket, blob


_gcs_client_singleton = None


def _gcs_client():
    """Reused across calls -- constructing storage.Client() repeatedly redoes
ADC credential discovery and was the dominant cost of dashboard() requests.
"""

    global _gcs_client_singleton
    if _gcs_client_singleton is None:
        from google.cloud import storage
        _gcs_client_singleton = storage.Client()
    return _gcs_client_singleton


def _read_gcs_text(path: str) -> str:
    bucket_name, blob_name = _parse_gcs_uri(path)
    client = _gcs_client()
    blob = client.bucket(bucket_name).blob(blob_name)
    return blob.download_as_text()


def _join(bucket_root: str, *parts: str) -> str:
    if _is_gcs_path(bucket_root):
        return "/".join([bucket_root.rstrip("/")] + [part.strip("/") for part in parts])
    return os.path.join(bucket_root, *parts)


def _exists(path: str) -> bool:
    if _is_gcs_path(path):
        bucket_name, blob_name = _parse_gcs_uri(path)
        client = _gcs_client()
        return client.bucket(bucket_name).blob(blob_name).exists()
    return os.path.exists(path)


def _is_dir(path: str) -> bool:
    if _is_gcs_path(path):
        bucket_name, prefix = _parse_gcs_uri(path)
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        prefix = prefix.rstrip("/") + "/"
        return any(True for _ in bucket.list_blobs(prefix=prefix, max_results=1))
    return os.path.isdir(path)


def _load(path):
    if _is_gcs_path(path):
        return json.loads(_read_gcs_text(path))
    with open(path) as f:
        return json.load(f)


def _join(bucket_root: str, *parts: str) -> str:
    if _is_gcs_path(bucket_root):
        return "/".join([bucket_root.rstrip("/")] + [part.strip("/") for part in parts])
    return os.path.join(bucket_root, *parts)


def is_complete_snapshot(bucket_root: str, persona: str, run_date: str) -> bool:
    """True once this date folder's pipeline run has actually finished writing.
"""

    if _exists(_join(bucket_root, persona, run_date, "marketing_heatmap.json")):
        return True
    if (_exists(_join(bucket_root, persona, run_date, "reminders_sent_monday_upcoming.json"))
            and not _exists(_join(bucket_root, persona, run_date, "inflow_flags.json"))):
        return True
    return False


_LATEST_RUN_DATE_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_LATEST_RUN_DATE_TTL_SECONDS = 30
# Every /{persona}/<endpoint> request independently resolves "today's run
# date" via this function. On GCS that means a list_blobs() call plus one or
# more blob.exists() probes -- real network round trips.

def latest_run_date(bucket_root: str, persona: str) -> str | None:
    """Most recent COMPLETE snapshot date folder (skips in-progress runs).

Cached for _LATEST_RUN_DATE_TTL_SECONDS per (bucket_root, persona) to avoid
repeating the underlying GCS list_blobs()/exists() round trips on every one
of the several concurrent per-tenant API calls the UI fires on each load.
"""

    cache_key = (bucket_root, persona)
    cached = _LATEST_RUN_DATE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _LATEST_RUN_DATE_TTL_SECONDS:
        return cached[1]

    persona_dir = os.path.join(bucket_root, persona)
    if _is_gcs_path(persona_dir):
        bucket_name, prefix = _parse_gcs_uri(persona_dir)
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        root_prefix = prefix.rstrip("/") + "/"
        # delimiter="/" makes GCS return only immediate subfolder names instead
        iterator = bucket.list_blobs(prefix=root_prefix, delimiter="/")
        list(iterator)  # must be consumed for .prefixes to populate
        dates = {
            p[len(root_prefix):].rstrip("/")
            for p in iterator.prefixes
            if not p[len(root_prefix):].rstrip("/").startswith("_")
        }
    else:
        if not os.path.isdir(persona_dir):
            _LATEST_RUN_DATE_CACHE[cache_key] = (now, None)
            return None
        dates = {d for d in os.listdir(persona_dir)
                 if os.path.isdir(os.path.join(persona_dir, d)) and not d.startswith("_")}

    result = None
    for d in sorted(dates, reverse=True):
        if is_complete_snapshot(bucket_root, persona, d):
            result = d
            break
    _LATEST_RUN_DATE_CACHE[cache_key] = (now, result)
    return result


def _resolve(bucket_root, persona, run_date):
    return run_date or latest_run_date(bucket_root, persona)


def top_counterparties(bucket_root: str, persona: str, run_date: str | None = None,
                       limit: int = 10) -> list[dict]:
    """Return ledger rows in the schema expected by the standalone UI."""
    run_date = _resolve(bucket_root, persona, run_date)
    if not run_date:
        return []
    path = _join(bucket_root, persona, run_date, "normalized_transactions.json")
    if _is_gcs_path(path):
        # We can only test existence by attempting to read or metadata lookup.
        try:
            _read_gcs_text(path)
        except Exception:
            return []
    else:
        if not os.path.exists(path):
            return []
    stats_path = _join(bucket_root, persona, run_date, "historical_stats.json")
    stats = _load(stats_path) if _exists(stats_path) else {}
    report_path = _join(bucket_root, persona, run_date, "top_risk_report.json")
    report = _load(report_path) if _exists(report_path) else {}
    risk_by_account: dict[str, tuple[str, float]] = {}
    for case in report.get("top_transactions", []):
        account = case.get("customer_reference_id")
        score = float(case.get("risk_score") or 0)
        if account and score >= risk_by_account.get(account, ("low", -1))[1]:
            risk_by_account[account] = (str(case.get("risk_tier") or "low").lower(), score)

    agg = defaultdict(lambda: {
        "transaction_count": 0, "total_value": 0.0,
        "counterparty_name": None, "dates": []
    })
    for t in _load(path):
        if t.get("status") not in (None, "Success"):
            continue
        cust = t.get("customer_reference_id")
        if not cust:
            continue
        a = agg[cust]
        a["transaction_count"] += 1
        a["total_value"] += float(t.get("amount") or 0)
        if not a["counterparty_name"] and t.get("counterparty_name"):
            a["counterparty_name"] = t["counterparty_name"]
        tx_date = t.get("transaction_date") or t.get("value_date")
        if tx_date:
            a["dates"].append(tx_date)

    rows = []
    for account, value in agg.items():
        account_stats = stats.get(account, {})
        dates = sorted(value["dates"])
        last_tx = dates[-1] if dates else account_stats.get("last_transaction_date")
        gaps = []
        for previous, current in zip(dates, dates[1:]):
            gaps.append((datetime.fromisoformat(current) - datetime.fromisoformat(previous)).days)
        cadence = round(sorted(gaps)[len(gaps) // 2]) if gaps else 7
        overdue = 0
        if last_tx:
            overdue = max(0, (date.fromisoformat(run_date) - date.fromisoformat(last_tx)).days - cadence)
        rows.append({
            "id": account,
            "customer_reference_id": account,
            "shortId": account.split("-")[-1],
            "name": value["counterparty_name"] or account,
            "counterparty_name": value["counterparty_name"] or account,
            "cadence": cadence,
            "lastTx": last_tx or "N/A",
            "overdue": overdue,
            "r3": round(float(account_stats.get("rolling_3m_avg") or 0), 2),
            "r6": round(float(account_stats.get("rolling_6m_avg") or 0), 2),
            "r12": round(float(account_stats.get("rolling_12m_avg") or 0), 2),
            "riskTier": risk_by_account.get(account, ("low", 0))[0],
            "transaction_count": value["transaction_count"],
            "total_value": round(value["total_value"], 2),
        })
    rows.sort(key=lambda r: r["total_value"], reverse=True)
    return rows[:limit]


def cases_awaiting_review(bucket_root: str, persona: str, run_date: str | None = None,
                          limit: int = 10) -> list[dict]:
    """Flagged cases awaiting analyst review, highest-risk first. Reads the
persisted top_risk_report.json when present; otherwise rebuilds it from the
"""

    run_date = _resolve(bucket_root, persona, run_date)
    if not run_date:
        return []
    prefix = _join(bucket_root, persona, run_date)
    report_path = _join(bucket_root, persona, run_date, "top_risk_report.json")
    if _exists(report_path):
        cases = _load(report_path).get("top_transactions", [])
    else:
        flags_path = _join(bucket_root, persona, run_date, "inflow_flags.json")
        norm_path = _join(bucket_root, persona, run_date, "normalized_transactions.json")
        if not _exists(flags_path):
            return []
        transactions = _load(norm_path) if _exists(norm_path) else []
        cases = top_risk.build_top_risk_report(_load(flags_path), transactions)

    merged = []
    for c in cases[:limit]:
        action = case_actions.load_case_action(persona, c.get("case_id"))
        if action and not case_actions.is_action_expired(action):
            merged.append({**c, "status": action["status"],
                           "reviewed_at": action.get("reviewed_at"),
                           "escalation": action.get("escalation")})
        else:
            merged.append({**c, "status": "awaiting_review"})
    return merged


def recent_notifications(bucket_root: str, persona: str, today: date | str,
                         limit: int = 10) -> list[dict]:
    """Last-4-weeks top-10 upcoming/missed notifications -- reuses notifications.py.
"""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    return notifications.last_4_weeks_notifications(bucket_root, persona, today, top_n=limit)


_firestore_audit_store_singleton = None


def _build_audit_store(audit_path: str):
    """GOVERNANCE_STORE-aware, mirroring orchestrator/nodes.py's and
api/main.py's _build_governance_stores() -- firestore in the cloud (shared
across the agent/api containers' separate filesystems), local file for dev.
"""

    from audit_store import LocalFileAuditStore

    if os.environ.get("GOVERNANCE_STORE", "memory").lower() == "firestore":
        global _firestore_audit_store_singleton
        if _firestore_audit_store_singleton is None:
            try:
                from audit_store import FirestoreAuditStore
                _firestore_audit_store_singleton = FirestoreAuditStore()
            except Exception as e:
                # Keep the governance audit endpoints serving (from the local
                # JSONL log) instead of 500ing when Firestore is unavailable.
                import sys
                print(f"[exports] Firestore audit store unavailable -- serving "
                      f"the local JSONL audit log instead. Cause: {e}", file=sys.stderr)
                return LocalFileAuditStore(audit_path)
        return _firestore_audit_store_singleton
    return LocalFileAuditStore(audit_path)


def governance_audit_summary(audit_path: str | None = None) -> dict:
    """Rollup of the immutable governance audit trail: total events, a
breakdown by agent and by stop_reason, and an explicit count of fail-closed
"""

    records = _build_audit_store(audit_path or DEFAULT_AUDIT_PATH).all()

    by_agent: dict[str, int] = defaultdict(int)
    by_agent_detail: dict[str, dict[str, int]] = defaultdict(lambda: {
        "calls": 0, "tokens": 0, "cache_hits": 0, "completed": 0,
    })
    by_stop_reason: dict[str, int] = defaultdict(int)
    for r in records:
        agent = r.get("agent", "unknown")
        by_agent[agent] += 1
        detail = by_agent_detail[agent]
        detail["calls"] += 1
        detail["tokens"] += int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
        if r.get("stop_reason") == "cache_hit":
            detail["cache_hits"] += 1
        if r.get("stop_reason") == "completed":
            detail["completed"] += 1
        by_stop_reason[r.get("stop_reason", "unknown")] += 1
    return {
        "total_events": len(records),
        "by_agent": dict(by_agent),
        "by_agent_detail": dict(by_agent_detail),
        "by_stop_reason": dict(by_stop_reason),
        "pii_blocks": by_stop_reason.get("pii_detected_blocked", 0),
        "llm_calls_completed": by_stop_reason.get("completed", 0),
        "cache_hits": by_stop_reason.get("cache_hit", 0),
        "cache_store": "Firestore" if os.environ.get("GOVERNANCE_STORE", "memory").lower() == "firestore" else "Local file",
        "audit_storage": "Firestore" if os.environ.get("GOVERNANCE_STORE", "memory").lower() == "firestore" else "Immutable JSONL",
    }


def governance_audit_log(audit_path: str | None = None, limit: int = 50) -> list[dict]:
    """Individual audit events, newest first -- the per-row detail behind
governance_audit_summary()'s rollup, for the UI's audit-log table.
"""

    records = _build_audit_store(audit_path or DEFAULT_AUDIT_PATH).all()
    records.sort(key=lambda r: r.get("timestamp") or 0, reverse=True)
    return [
        {
            "trace_id": r.get("trace_id"),
            "agent": r.get("agent"),
            "model": r.get("model"),
            "input_tokens": r.get("input_tokens", 0),
            "output_tokens": r.get("output_tokens", 0),
            "latency_ms": r.get("latency_ms", 0),
            "stop_reason": r.get("stop_reason"),
        }
        for r in records[:limit]
    ]


def marketing_heatmap(bucket_root: str, persona: str, run_date: str | None = None) -> list[dict]:
    """The marketing agent's per-postal-code heatmap for a snapshot (reuses its
persisted marketing_heatmap.json).
"""

    run_date = _resolve(bucket_root, persona, run_date)
    if not run_date:
        return []
    path = _join(bucket_root, persona, run_date, "marketing_heatmap.json")
    if not _exists(path):
        return []
    return _load(path).get("heatmap", [])


def dashboard(bucket_root: str, persona: str, today: date | str | None = None,
              audit_path: str | None = None) -> dict:
    """Everything a per-persona overview screen needs, in one call.
"""
    run_date = latest_run_date(bucket_root, persona)
    today = today or (run_date or date.today().isoformat())
    risk = {"tier": "low", "reasoning": "No risk assessment available."}
    risk_path = _join(bucket_root, persona, run_date, "top_risk_report.json") if run_date else None
    if risk_path and _exists(risk_path):
        report = _load(risk_path)
        risk["tier"] = str(report.get("run_risk_tier") or "low").lower()
        cases = report.get("top_transactions") or []
        if cases:
            risk["reasoning"] = cases[0].get("reason") or risk["reasoning"]
    counterparties = top_counterparties(bucket_root, persona, run_date, limit=100000)
    cases = cases_awaiting_review(bucket_root, persona, run_date, limit=100000)
    heatmap = marketing_heatmap(bucket_root, persona, run_date)
    overdue = [row for row in counterparties if row.get("overdue", 0) > 0]
    metrics = {
        "total_accounts": len(counterparties),
        "average_cadence_days": round(
            sum(float(row.get("cadence") or 0) for row in counterparties) / len(counterparties), 1
        ) if counterparties else 0,
        "high_risk_accounts": sum(row.get("riskTier") == "high" for row in counterparties),
        "overdue_count": len(overdue),
        "longest_overdue_days": max((row.get("overdue", 0) for row in overdue), default=0),
        "overdue_capital": round(sum(float(row.get("r3") or 0) for row in overdue), 2),
        "projected_inflow": round(sum(float(row.get("r3") or 0) for row in counterparties), 2),
    }
    # Keep the marketing panel data-backed.  The heatmap is the persisted output
    # of the marketing agent, so it is a better source than browser-side demo
    # narratives and remains available while a new pipeline run is processing.
    opportunities = sorted(heatmap, key=lambda item: float(item.get("total_value") or 0), reverse=True)
    top = opportunities[0] if opportunities else {}
    products = []
    if top:
        region = top.get("city") or top.get("postal_code") or "the leading region"
        volume = round(float(top.get("total_value") or 0), 2)
        products.append({
            "name": "Working-capital facility",
            "rationale": f"Latest activity in {region} totals EUR {volume:,.2f}; assess a facility sized to the observed inflow pattern.",
        })
        if any(row.get("riskTier") == "high" for row in counterparties) or overdue:
            products.append({
                "name": "Trade credit insurance",
                "rationale": "Live ledger data shows overdue or high-risk counterparties; protect receivables while exposure is reviewed.",
            })
        products.append({
            "name": "Invoice factoring",
            "rationale": f"Use the {region} inflow concentration as a candidate for faster receivables conversion.",
        })
    marketing = {
        "pattern_label": "Regional inflow opportunity" if top else "No opportunity signal available",
        "detail": (top.get("narrative") or "No marketing narrative was persisted for the latest snapshot."),
        "products": products,
        "top_region": top.get("city") or top.get("postal_code"),
        "top_region_metric": round(float(top.get("total_value") or 0), 2) if top else 0,
        "network_count": len(heatmap),
    }
    return {
        "risk": risk,
        "persona": persona,
        "run_date": run_date,
        "metrics": metrics,
        "marketing": marketing,
        "top_counterparties": counterparties,
        "cases_awaiting_review": cases,
        "recent_notifications": recent_notifications(bucket_root, persona, today),
        "marketing_heatmap": heatmap,
        "governance_audit_summary": governance_audit_summary(audit_path),
    }
