"""
SlipIQ Database — Supabase persistence
Falls back to local JSON when SUPABASE_URL / SUPABASE_KEY are not set.
"""

import json
import os
import shutil
from datetime import datetime, date

RESULTS_FILE = "slipiq_results.json"
_client = None
_supabase_import_ok = None
_supabase_warned = False


def _supabase_available():
    """True only if package is installed (can fail on Python 3.14 without build tools)."""
    global _supabase_import_ok
    if _supabase_import_ok is None:
        try:
            from supabase import create_client  # noqa: F401
            _supabase_import_ok = True
        except ImportError:
            _supabase_import_ok = False
    return _supabase_import_ok


def is_configured():
    return bool(
        os.getenv("SUPABASE_URL")
        and os.getenv("SUPABASE_KEY")
        and _supabase_available()
    )


def get_client():
    global _client
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_KEY"):
        return None
    if not _supabase_available():
        global _supabase_warned
        if not _supabase_warned:
            print(
                "Supabase env set but 'supabase' package not installed - using local JSON only. "
                "Run: pip install supabase  (or remove SUPABASE_* from .env)"
            )
            _supabase_warned = True
        return None
    if _client is None:
        from supabase import create_client

        _client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _client


def _entry_to_row(entry):
    """Map local pick entry to Supabase row."""
    return {
        "pick_date": entry["date"],
        "pitcher": entry["pitcher"],
        "direction": entry["direction"],
        "line": float(entry["line"]),
        "projection": float(entry.get("projection", 0)),
        "grade": entry.get("grade"),
        "confidence": float(entry.get("confidence", 0)),
        "model_confidence": float(entry.get("model_confidence", entry.get("confidence", 0))),
        "ev_score": float(entry["ev_score"]) if entry.get("ev_score") is not None else None,
        "trend": entry.get("trend"),
        "bookmaker": entry.get("bookmaker"),
        "result": entry.get("result"),
        "actual_strikeouts": entry.get("actual_strikeouts"),
        "actual_game_date": entry.get("actual_game_date"),
        "logged_at": entry.get("logged_at"),
        "updated_at": entry.get("updated_at"),
        "slip_review": entry.get("slip_review"),
        "settled_by": entry.get("settled_by"),
    }


def _row_to_entry(row):
    """Map Supabase row to local pick entry."""
    return {
        "date": row["pick_date"],
        "pitcher": row["pitcher"],
        "direction": row["direction"],
        "line": float(row["line"]),
        "projection": float(row.get("projection", 0)),
        "grade": row.get("grade"),
        "confidence": float(row.get("confidence", 0)),
        "model_confidence": float(row.get("model_confidence", row.get("confidence", 0))),
        "ev_score": row.get("ev_score"),
        "trend": row.get("trend"),
        "bookmaker": row.get("bookmaker"),
        "result": row.get("result"),
        "actual_strikeouts": row.get("actual_strikeouts"),
        "actual_game_date": row.get("actual_game_date"),
        "logged_at": row.get("logged_at"),
        "updated_at": row.get("updated_at"),
        "slip_review": row.get("slip_review"),
        "settled_by": row.get("settled_by"),
    }


def to_json_safe(value):
    """Convert numpy/pandas scalars to plain Python types for json.dump."""
    if value is None:
        return None
    if type(value) is bool:
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    # numpy.bool_, numpy.float64, etc. (type(bool) is not numpy.bool_)
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return to_json_safe(value.item())
    except ImportError:
        pass
    if hasattr(value, "item"):
        try:
            return to_json_safe(value.item())
        except (ValueError, TypeError):
            pass
    return value


def load_results_json():
    if not os.path.exists(RESULTS_FILE):
        return []
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        backup = RESULTS_FILE + ".corrupt.bak"
        shutil.copy2(RESULTS_FILE, backup)
        print(
            f"WARNING: {RESULTS_FILE} was corrupt ({e}). "
            f"Backed up to {backup}. Starting with empty results."
        )
        return []


def save_results_json(results):
    safe = to_json_safe(results)
    tmp_path = RESULTS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2)
    os.replace(tmp_path, RESULTS_FILE)


def load_results():
    """Load all picks from Supabase or local JSON."""
    client = get_client()
    if not client:
        return load_results_json()

    try:
        resp = (
            client.table("picks")
            .select("*")
            .order("pick_date", desc=True)
            .execute()
        )
        return [_row_to_entry(r) for r in resp.data]
    except Exception as e:
        print(f"Supabase load failed, using JSON: {e}")
        return load_results_json()


def save_results(results):
    """Persist full list — JSON always; Supabase syncs each row on log/update."""
    save_results_json(results)


def upsert_pick_entry(entry):
    """Insert or update one pick in Supabase."""
    client = get_client()
    if not client:
        return False

    row = _entry_to_row(entry)
    try:
        client.table("picks").upsert(
            row,
            on_conflict="pick_date,pitcher,direction,line",
        ).execute()
        return True
    except Exception as e:
        print(f"Supabase upsert failed: {e}")
        return False


def sync_json_to_supabase():
    """One-time migration: push local JSON history to Supabase."""
    client = get_client()
    if not client:
        print("Supabase not configured")
        return 0

    results = load_results_json()
    if not results:
        print("No local picks to sync")
        return 0

    ok = 0
    for entry in results:
        if upsert_pick_entry(entry):
            ok += 1
    print(f"Synced {ok}/{len(results)} picks to Supabase")
    return ok


def pick_entry_from_log(pick, result=None):
    """Build a results entry dict from a pipeline pick."""
    today = date.today().strftime("%Y-%m-%d")
    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
    grade = pick.get("grade") or pick["recommendation"].split("Grade: ")[-1].split(" |")[0].strip()

    entry = {
        "date": today,
        "pitcher": pick["pitcher"],
        "direction": direction,
        "line": pick["line"],
        "projection": pick["projection"],
        "grade": grade,
        "confidence": pick.get("display_confidence", pick["confidence"]),
        "model_confidence": pick.get("model_confidence", pick["confidence"]),
        "trend": pick["trend"],
        "bookmaker": pick["bookmaker"],
        "result": result,
        "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if pick.get("ev_score") is not None:
        entry["ev_score"] = pick["ev_score"]
    if pick.get("slip_review"):
        entry["slip_review"] = to_json_safe(pick["slip_review"])
    return to_json_safe(entry)


if __name__ == "__main__":
    import sys

    if "--sync" in sys.argv:
        sync_json_to_supabase()
    else:
        print("Supabase configured:", is_configured())
        print("Local picks:", len(load_results_json()))
        if is_configured():
            print("Remote picks:", len(load_results()))
