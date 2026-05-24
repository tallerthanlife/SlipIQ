"""
SlipIQ Database — Supabase persistence
Falls back to local JSON when SUPABASE_URL / SUPABASE_KEY are not set.
"""

import json
import os
from datetime import datetime, date

RESULTS_FILE = "slipiq_results.json"
_client = None


def is_configured():
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"))


def get_client():
    global _client
    if not is_configured():
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


def load_results_json():
    if not os.path.exists(RESULTS_FILE):
        return []
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_results_json(results):
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


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
        entry["slip_review"] = pick["slip_review"]
    return entry


if __name__ == "__main__":
    import sys

    if "--sync" in sys.argv:
        sync_json_to_supabase()
    else:
        print("Supabase configured:", is_configured())
        print("Local picks:", len(load_results_json()))
        if is_configured():
            print("Remote picks:", len(load_results()))
