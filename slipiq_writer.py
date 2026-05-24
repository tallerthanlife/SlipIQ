"""
SlipIQ Writer — Groq Powered
Generates pick writeups and analysis using Groq's free API
Replaces Anthropic — same output, zero cost
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ─── Core Groq Call ───────────────────────────────────────────

def call_groq(prompt, system_prompt=None, max_tokens=500):
    """
    Core function — calls Groq API with any prompt
    Returns text response or error string
    """
    if not GROQ_API_KEY:
        return "ERROR: GROQ_API_KEY not set in .env"

    system = system_prompt or (
        "You are SlipIQ, an elite MLB prop betting analyst. "
        "You give sharp, data-driven analysis in 2-3 sentences max. "
        "No fluff. No disclaimers. Just the edge."
    )

    try:
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    except requests.exceptions.Timeout:
        return "Analysis unavailable — API timeout"
    except Exception as e:
        return f"Analysis unavailable — {e}"

# ─── Pick Writeup ─────────────────────────────────────────────

def generate_pick_writeup(pick):
    """
    Generate a 2-3 sentence writeup for a single pick
    pick = dict from slipiq_lines.run_full_analysis()
    """
    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
    grade = pick["recommendation"].split("Grade: ")[-1].split(" |")[0].strip()

    prompt = f"""
Pitcher: {pick['pitcher']}
Pick: {direction} {pick['line']} strikeouts
Projection: {pick['projection']} K
Season Average: {pick.get('season_avg', 'N/A')} K
Last 3 Starts: {pick.get('last_3_avg', 'N/A')} K
Trend: {pick['trend']}
Confidence: {pick['confidence']}%
Grade: {grade}

Write a sharp 2-sentence analysis explaining why this is the play today.
Focus on the data edge. Be direct and confident.
"""
    return call_groq(prompt)

# ─── Daily Brief ──────────────────────────────────────────────

def generate_daily_brief(picks):
    """
    Generate a one-paragraph daily brief summarizing today's slate
    """
    if not picks:
        return "No high-confidence picks today. Model found no clear edges."

    pick_summary = "\n".join([
        f"- {p['pitcher']}: {('OVER' if 'OVER' in p['recommendation'] else 'UNDER')} "
        f"{p['line']} K (proj: {p['projection']} K, {p['trend']})"
        for p in picks
    ])

    prompt = f"""
Today's SlipIQ MLB Pitcher Prop Picks:
{pick_summary}

Write a sharp 2-sentence daily brief summarizing today's slate and the overall lean.
Sound like an elite sports analyst. Be concise and confident.
"""
    return call_groq(prompt, max_tokens=200)

# ─── Confidence Score ─────────────────────────────────────────

def generate_confidence_score(pick):
    """
    Use Groq to give an agentic second opinion on confidence
    Returns adjusted confidence score (0-100) as integer
    """
    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"

    prompt = f"""
You are a sharp sports betting analyst reviewing a model's pick.

Pitcher: {pick['pitcher']}
Pick: {direction} {pick['line']} K
Model Projection: {pick['projection']} K  
Season Avg: {pick.get('season_avg', 'N/A')} K
Last 3 Starts Avg: {pick.get('last_3_avg', 'N/A')} K
Trend: {pick['trend']}
Model Confidence: {pick['confidence']}%

Based on this data, respond with ONLY a single integer between 0 and 100 
representing your adjusted confidence in this pick. Nothing else. Just the number.
"""
    result = call_groq(prompt, max_tokens=10)
    
    # Extract integer from response
    try:
        score = int(''.join(filter(str.isdigit, result)))
        return min(99, max(1, score))
    except:
        return pick["confidence"]  # fall back to model score

# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ Writer — Groq Test ===\n")

    # Test with a sample pick
    test_pick = {
        "pitcher": "Zac Gallen",
        "line": 5.5,
        "projection": 3.0,
        "recommendation": "UNDER 5.5 | Grade: A | Confidence: 76.1%",
        "confidence": 76.1,
        "trend": "NEUTRAL",
        "season_avg": 4.2,
        "last_3_avg": 3.1,
        "bookmaker": "FanDuel"
    }

    print("Testing pick writeup...")
    writeup = generate_pick_writeup(test_pick)
    print(f"Writeup: {writeup}\n")

    print("Testing confidence score...")
    score = generate_confidence_score(test_pick)
    print(f"Adjusted confidence: {score}%\n")

    print("Testing daily brief...")
    brief = generate_daily_brief([test_pick])
    print(f"Brief: {brief}")