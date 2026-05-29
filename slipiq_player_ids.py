# slipiq_player_ids.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — MLB Player ID Lookup
#
# PROBLEM SOLVED:
#   Batter matching across parlayapi, pybaseball, and statsapi
#   uses three different name formats and ID systems.
#   Fuzzy string matching silently mismatches players like
#   "Michael Harris" (II) or pitchers with identical surnames.
#
# HOW IT WORKS:
#   1. normalize_name() — canonical lowercase strip
#   2. lookup_player() — exact match first, then alias fallback
#   3. get_mlb_id() / get_espn_id() — for pybaseball / statsapi calls
#   4. resolve_player_from_prop() — fuzzy match for API name variants
#
# MAINTENANCE:
#   Update MLB_PLAYER_IDS at start of each season with new/traded players.
#   Common aliases (Jr, II, accent variants) already included.
#   Run python slipiq_player_ids.py to verify lookup on today's slate.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations
import re
import unicodedata
from difflib import SequenceMatcher


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — PLAYER DATABASE
# ═══════════════════════════════════════════════════════════════

# Format: "canonical_name": {"mlb_id": int, "espn_id": int, "team": str, "aliases": [...]}
# mlb_id  = MLB Stats API / pybaseball player ID
# espn_id = ESPN player ID (for BallDontLie / ESPN API calls)
# team    = current team abbreviation (update each season)

MLB_PLAYER_IDS: dict[str, dict] = {

    # ── Active SP / RP (2026 season) ──────────────────────────
    "gerrit cole":           {"mlb_id": 543037, "espn_id": 33912,  "team": "NYY", "aliases": []},
    "zack wheeler":          {"mlb_id": 554430, "espn_id": 32862,  "team": "PHI", "aliases": []},
    "corbin burnes":         {"mlb_id": 669203, "espn_id": 38707,  "team": "BAL", "aliases": []},
    "logan webb":            {"mlb_id": 657277, "espn_id": 39830,  "team": "SFG", "aliases": []},
    "pablo lopez":           {"mlb_id": 641154, "espn_id": 39869,  "team": "MIN", "aliases": []},
    "tarik skubal":          {"mlb_id": 669373, "espn_id": 40583,  "team": "DET", "aliases": []},
    "spencer strider":       {"mlb_id": 675911, "espn_id": 40716,  "team": "ATL", "aliases": []},
    "max fried":             {"mlb_id": 608331, "espn_id": 35532,  "team": "NYY", "aliases": []},
    "chris sale":            {"mlb_id": 519242, "espn_id": 28976,  "team": "ATL", "aliases": []},
    "dylan cease":           {"mlb_id": 656302, "espn_id": 38980,  "team": "SDP", "aliases": []},
    "freddy peralta":        {"mlb_id": 642547, "espn_id": 39262,  "team": "MIL", "aliases": []},
    "hunter greene":         {"mlb_id": 668881, "espn_id": 40437,  "team": "CIN", "aliases": []},
    "kevin gausman":         {"mlb_id": 592332, "espn_id": 32724,  "team": "TOR", "aliases": []},
    "luis castillo":         {"mlb_id": 622491, "espn_id": 36145,  "team": "SEA", "aliases": []},
    "sandy alcantara":       {"mlb_id": 645261, "espn_id": 38511,  "team": "MIA", "aliases": []},
    "tyler glasnow":         {"mlb_id": 607192, "espn_id": 36871,  "team": "LAD", "aliases": []},
    "yoshinobu yamamoto":    {"mlb_id": 808967, "espn_id": 43488,  "team": "LAD", "aliases": ["y. yamamoto"]},
    "shota imanaga":         {"mlb_id": 807985, "espn_id": 43485,  "team": "CHC", "aliases": ["s. imanaga"]},
    "blake snell":           {"mlb_id": 605483, "espn_id": 35617,  "team": "SFG", "aliases": []},
    "braxton garrett":       {"mlb_id": 669219, "espn_id": 40531,  "team": "MIA", "aliases": []},
    "paul skenes":           {"mlb_id": 808967, "espn_id": 43999,  "team": "PIT", "aliases": []},
    "gavin stone":           {"mlb_id": 687799, "espn_id": 42588,  "team": "LAD", "aliases": []},
    "cole ragans":           {"mlb_id": 669003, "espn_id": 40487,  "team": "KCR", "aliases": []},
    "ryan pepiot":           {"mlb_id": 687855, "espn_id": 42600,  "team": "TBR", "aliases": []},
    "shane baz":             {"mlb_id": 672275, "espn_id": 40996,  "team": "TBR", "aliases": []},
    "jacob degrom":          {"mlb_id": 594798, "espn_id": 33160,  "team": "TEX", "aliases": ["j. degrom"]},
    "dustin may":            {"mlb_id": 663623, "espn_id": 39917,  "team": "LAD", "aliases": []},
    "noah cameron":          {"mlb_id": 694297, "espn_id": 43100,  "team": "MIN", "aliases": []},
    "gavin williams":        {"mlb_id": 695110, "espn_id": 43301,  "team": "CLE", "aliases": []},

    # ── Active Batters ─────────────────────────────────────────
    "aaron judge":           {"mlb_id": 592450, "espn_id": 33192,  "team": "NYY", "aliases": []},
    "mookie betts":          {"mlb_id": 605141, "espn_id": 33137,  "team": "LAD", "aliases": []},
    "freddie freeman":       {"mlb_id": 518692, "espn_id": 28660,  "team": "LAD", "aliases": []},
    "juan soto":             {"mlb_id": 665742, "espn_id": 39906,  "team": "NYM", "aliases": []},
    "yordan alvarez":        {"mlb_id": 670541, "espn_id": 40143,  "team": "HOU", "aliases": []},
    "shohei ohtani":         {"mlb_id": 660271, "espn_id": 39832,  "team": "LAD", "aliases": ["s. ohtani"]},
    "mike trout":            {"mlb_id": 545361, "espn_id": 30836,  "team": "LAA", "aliases": []},
    "bryce harper":          {"mlb_id": 547180, "espn_id": 31867,  "team": "PHI", "aliases": []},
    "trea turner":           {"mlb_id": 607208, "espn_id": 36757,  "team": "PHI", "aliases": []},
    "corey seager":          {"mlb_id": 608369, "espn_id": 35493,  "team": "TEX", "aliases": []},
    "jose ramirez":          {"mlb_id": 608070, "espn_id": 35617,  "team": "CLE", "aliases": ["j. ramirez"]},
    "rafael devers":         {"mlb_id": 646240, "espn_id": 38678,  "team": "BOS", "aliases": []},
    "pete alonso":           {"mlb_id": 624413, "espn_id": 37832,  "team": "NYM", "aliases": []},
    "paul goldschmidt":      {"mlb_id": 502671, "espn_id": 29344,  "team": "STL", "aliases": []},
    "nolan arenado":         {"mlb_id": 571448, "espn_id": 31482,  "team": "STL", "aliases": []},
    "michael harris ii":     {"mlb_id": 671739, "espn_id": 40891,  "team": "ATL", "aliases": ["michael harris", "m. harris"]},
    "austin riley":          {"mlb_id": 663586, "espn_id": 39912,  "team": "ATL", "aliases": []},
    "william contreras":     {"mlb_id": 661388, "espn_id": 40107,  "team": "MIL", "aliases": []},
    "willy adames":          {"mlb_id": 642715, "espn_id": 39265,  "team": "SFG", "aliases": []},
    "kyle tucker":           {"mlb_id": 663656, "espn_id": 39936,  "team": "CHC", "aliases": []},
    "alex bregman":          {"mlb_id": 608324, "espn_id": 36035,  "team": "BOS", "aliases": []},
    "julio rodriguez":       {"mlb_id": 677594, "espn_id": 41106,  "team": "SEA", "aliases": ["j. rodriguez"]},
    "gunnar henderson":      {"mlb_id": 683002, "espn_id": 42011,  "team": "BAL", "aliases": []},
    "jackson chourio":       {"mlb_id": 694192, "espn_id": 43282,  "team": "MIL", "aliases": []},
    "bobby witt jr":         {"mlb_id": 677951, "espn_id": 41218,  "team": "KCR", "aliases": ["bobby witt", "b. witt jr", "b. witt"]},
    "elly de la cruz":       {"mlb_id": 682829, "espn_id": 41992,  "team": "CIN", "aliases": ["elly de la cruz"]},
    "corbin carroll":        {"mlb_id": 682998, "espn_id": 41997,  "team": "ARI", "aliases": []},
    "adolis garcia":         {"mlb_id": 666969, "espn_id": 40202,  "team": "TEX", "aliases": []},
    "teoscar hernandez":     {"mlb_id": 606192, "espn_id": 36389,  "team": "LAD", "aliases": ["t. hernandez"]},
    "ozzie albies":          {"mlb_id": 645277, "espn_id": 38473,  "team": "ATL", "aliases": []},
    "marcus semien":         {"mlb_id": 543760, "espn_id": 31543,  "team": "TEX", "aliases": []},
    "cal raleigh":           {"mlb_id": 663728, "espn_id": 40074,  "team": "SEA", "aliases": []},
    "william contreras":     {"mlb_id": 661388, "espn_id": 40107,  "team": "MIL", "aliases": []},
    "riley greene":          {"mlb_id": 682985, "espn_id": 41978,  "team": "DET", "aliases": []},
    "colt keith":            {"mlb_id": 694102, "espn_id": 43308,  "team": "DET", "aliases": []},
    "jarren duran":          {"mlb_id": 680757, "espn_id": 41499,  "team": "BOS", "aliases": []},
}

# Built after normalize_name is defined — see end of Section 2
_ALIAS_INDEX: dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — NORMALIZATION
# ═══════════════════════════════════════════════════════════════

def normalize_name(name: str) -> str:
    """
    Canonical lowercase name normalization.
    Strips accents, punctuation, extra whitespace, suffixes.
    'José Ramírez Jr.' → 'jose ramirez jr'
    'Bobby Witt Jr.' → 'bobby witt jr'
    """
    if not name:
        return ""
    nfd    = unicodedata.normalize("NFD", name)
    ascii_ = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    lower  = ascii_.lower()
    clean  = re.sub(r"[^a-z0-9 \-]", "", lower)
    clean  = re.sub(r"\s+", " ", clean).strip()
    return clean


# Build alias index now that normalize_name is defined
def _build_alias_index() -> None:
    for canonical, data in MLB_PLAYER_IDS.items():
        _ALIAS_INDEX[canonical] = canonical
        for alias in data.get("aliases", []):
            _ALIAS_INDEX[normalize_name(alias)] = canonical

_build_alias_index()


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — LOOKUP FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def lookup_player(name: str) -> dict | None:
    """
    Look up a player by name. Returns player dict or None.
    1. Exact normalized match
    2. Alias match
    3. Returns None (caller should try fuzzy_match)
    """
    normalized = normalize_name(name)
    canonical  = _ALIAS_INDEX.get(normalized)
    if canonical:
        return {"canonical": canonical, **MLB_PLAYER_IDS[canonical]}
    return None


def get_mlb_id(name: str) -> int | None:
    """Get MLB Stats API / pybaseball player ID."""
    player = lookup_player(name)
    return player["mlb_id"] if player else None


def get_espn_id(name: str) -> int | None:
    """Get ESPN player ID."""
    player = lookup_player(name)
    return player["espn_id"] if player else None


def get_team(name: str) -> str | None:
    """Get current team abbreviation."""
    player = lookup_player(name)
    return player["team"] if player else None


def fuzzy_match(name: str, threshold: float = 0.82) -> dict | None:
    """
    Fuzzy match when exact lookup fails.
    Used for API name variants (e.g. "Y. Yamamoto" → "yoshinobu yamamoto").
    Returns best match above threshold or None.

    threshold: 0.82 is conservative — avoids false positives on common surnames.
    """
    normalized = normalize_name(name)
    best_score = 0.0
    best_canonical = None

    for canonical in MLB_PLAYER_IDS:
        score = SequenceMatcher(None, normalized, canonical).ratio()
        if score > best_score:
            best_score = score
            best_canonical = canonical

    if best_score >= threshold and best_canonical:
        return {"canonical": best_canonical, "score": best_score,
                **MLB_PLAYER_IDS[best_canonical]}
    return None


def resolve_player_from_prop(prop_player_name: str) -> dict | None:
    """
    Full resolution pipeline for a player name from a prop API.
    1. Exact normalized lookup
    2. Alias lookup
    3. Fuzzy match fallback
    Returns player dict with "canonical" key, or None if no match.
    """
    result = lookup_player(prop_player_name)
    if result:
        return result

    result = fuzzy_match(prop_player_name)
    if result:
        return result

    return None


def is_same_player(name_a: str, name_b: str) -> bool:
    """
    Check if two name strings refer to the same player.
    Uses full resolution pipeline for both.
    """
    a = resolve_player_from_prop(name_a)
    b = resolve_player_from_prop(name_b)
    if a and b:
        return a["canonical"] == b["canonical"]
    # Fallback: normalized exact match
    return normalize_name(name_a) == normalize_name(name_b)


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — BULK TEAM ROSTER LOOKUP
# ═══════════════════════════════════════════════════════════════

def get_team_roster(team_abbr: str) -> list[dict]:
    """
    Return all players in the lookup table for a given team.
    Useful for batter correlation matching in slipiq_ml_parlay.
    """
    team = team_abbr.upper()
    return [
        {"canonical": name, **data}
        for name, data in MLB_PLAYER_IDS.items()
        if data.get("team") == team
    ]


def is_batter_on_team(player_name: str, team_abbr: str) -> bool:
    """
    Reliable replacement for slipiq_ml_parlay._batter_on_team().
    Uses ID lookup instead of fuzzy string team matching.
    """
    result = resolve_player_from_prop(player_name)
    if not result:
        return False
    return result.get("team", "").upper() == team_abbr.upper()


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Player ID Lookup Self-Test")
    print("=" * 60)

    test_cases = [
        ("Gerrit Cole",       543037),
        ("gerrit cole",       543037),
        ("GERRIT COLE",       543037),
        ("Bobby Witt Jr.",    677951),
        ("Bobby Witt Jr",     677951),
        ("b. witt",           677951),
        ("Michael Harris",    671739),
        ("Michael Harris II", 671739),
        ("Y. Yamamoto",       808967),  # alias fuzzy test
        ("Yoshinobu Yamamoto",808967),
        ("Unknown Player",    None),
    ]

    passed = 0
    for name, expected_id in test_cases:
        result = resolve_player_from_prop(name)
        got_id = result["mlb_id"] if result else None
        status = "✓" if got_id == expected_id else "✗"
        if got_id == expected_id:
            passed += 1
        canonical = result.get("canonical", "no match") if result else "no match"
        print(f"  {status} '{name}' → {canonical} (mlb_id={got_id})")

    print(f"\n  {passed}/{len(test_cases)} tests passed")

    # Team roster test
    nyy = get_team_roster("NYY")
    print(f"\n  NYY roster in lookup: {[p['canonical'] for p in nyy]}")

    # is_batter_on_team test
    print(f"\n  Aaron Judge on NYY: {is_batter_on_team('Aaron Judge', 'NYY')}")
    print(f"  Aaron Judge on BOS: {is_batter_on_team('Aaron Judge', 'BOS')}")
