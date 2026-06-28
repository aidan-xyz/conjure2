"""
Conjure generation pipeline.

1. OpenAI Responses API + web search (prompt1) — research the person, return structured JSON
2. OpenAI chat completions (prompt2) — format the structured JSON into the homepage artifact JSON

Usage:
    from pipeline import run_pipeline
    result = run_pipeline("Jane Smith", "https://linkedin.com/in/janesmith", openai_key)
"""

import os
import json
import re
import httpx
from pathlib import Path

PROMPT1_PATH = Path(__file__).parent / "prompt1.txt"
PROMPT2_PATH = Path(__file__).parent / "prompt2.txt"


# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------

def _strip_code_fence(text: str) -> str:
    """Strip ```json ... ``` fences if the model wraps its output in them."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


def _call_openai_with_search(system_prompt: str, user_message: str, api_key: str) -> str:
    """
    Call the OpenAI Responses API with the web_search_preview tool.
    Used for prompt1 so the model looks up real, current facts instead of guessing.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "gpt-4o",
        "tools": [{"type": "web_search_preview"}],
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    with httpx.Client(timeout=180) as client:
        resp = client.post("https://api.openai.com/v1/responses", headers=headers, json=body)
        resp.raise_for_status()
    data = resp.json()
    # output_text is a convenience property on the response object
    text = data.get("output_text", "")
    if not text:
        # Fallback: walk output items
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text += part.get("text", "")
    return text


def _call_openai(system_prompt: str, user_message: str, api_key: str, model: str = "gpt-4o") -> str:
    """Regular chat completions with forced JSON output. Used for prompt2."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def structure_person_data(name: str, linkedin_url: str | None, openai_key: str) -> dict:
    """
    Run prompt1 with live web search: look up the real person, return structured JSON.
    Uses the Responses API so the model can actually search, not guess.
    """
    system_prompt = PROMPT1_PATH.read_text(encoding="utf-8")
    identifier = linkedin_url or name
    user_message = f"Find everything on: {identifier}"
    raw = _call_openai_with_search(system_prompt, user_message, openai_key)
    return json.loads(_strip_code_fence(raw))


def format_homepage_json(structured: dict, openai_key: str) -> dict:
    """Run prompt2: given structured person JSON, return homepage artifact JSON."""
    system_prompt = PROMPT2_PATH.read_text(encoding="utf-8")
    user_message = f"INPUT (raw blob):\n{json.dumps(structured, indent=2)}\n\nOUTPUT:"
    raw = _call_openai(system_prompt, user_message, openai_key)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(name: str, linkedin_url: str | None, openai_key: str) -> dict:
    """
    Full pipeline: name/URL → homepage artifact JSON.

    Args:
        name:         Person's full name (always required as fallback).
        linkedin_url: LinkedIn profile URL (optional but strongly recommended).
        openai_key:   OpenAI API key (sk-...).

    Returns:
        Homepage artifact JSON dict (ready to serialise or pass to a renderer).
    """
    if not name and not linkedin_url:
        raise ValueError("At least one of name or linkedin_url is required.")

    structured = structure_person_data(name, linkedin_url, openai_key)
    homepage = format_homepage_json(structured, openai_key)
    return homepage


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import pprint

    _name = sys.argv[1] if len(sys.argv) > 1 else input("Full name: ")
    _url = sys.argv[2] if len(sys.argv) > 2 else input("LinkedIn URL (blank to skip): ").strip() or None
    _oai = os.environ.get("OPENAI_API_KEY") or input("OpenAI API key: ")

    result = run_pipeline(_name, _url, _oai)
    pprint.pprint(result)

