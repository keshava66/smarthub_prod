"""
Quick check of what the assistant actually resolved from backend/.env.
Run from the backend dir:  python -m assistant.check_env

Keys are masked. The point is to see the chat/embed pairing and catch the case
where an embed var is missing and silently falls back to the chat one.
"""

import os
from . import env  # noqa: F401  -- loads backend/.env


def mask(v):
    if not v:
        return "(missing)"
    return f"...{v[-4:]}  (len {len(v)})"


def embed(name, default=None):
    own = os.getenv(f"AZURE_OPENAI_EMBED_{name}")
    generic = os.getenv(f"AZURE_OPENAI_{name}", default)
    val = own or generic
    fell_back = (not own) and bool(generic)
    return val, fell_back


print("CHAT resource")
print("  endpoint   :", os.getenv("AZURE_OPENAI_ENDPOINT") or "(missing)")
print("  api_version:", os.getenv("AZURE_OPENAI_API_VERSION") or "(default)")
print("  deployment :", os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT") or "(default gpt-4o)")
print("  api_key    :", mask(os.getenv("AZURE_OPENAI_API_KEY")))

print("\nEMBED resource")
for label, name in [("endpoint", "ENDPOINT"), ("api_version", "API_VERSION"), ("deployment", "DEPLOYMENT")]:
    val, fb = embed(name)
    note = "  <-- FELL BACK TO CHAT VALUE" if fb else ""
    print(f"  {label:11}: {val or '(missing)'}{note}")
key, fb = embed("API_KEY")
print(f"  api_key    : {mask(key)}{'  <-- FELL BACK TO CHAT KEY' if fb else ''}")

print("\nTTS (avatar voice)")
tts_key = os.getenv("AZURE_OPENAI_TTS_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY")
print("  endpoint   :", os.getenv("AZURE_OPENAI_TTS_ENDPOINT") or "(default J&J audio gateway)")
print("  deployment :", os.getenv("AZURE_OPENAI_TTS_DEPLOYMENT") or "(default)")
print("  voice      :", os.getenv("AZURE_OPENAI_TTS_VOICE") or "(default)")
print("  api_key    :", mask(tts_key))
if not tts_key:
    print("  no key -> /assistant/tts returns 503 and the browser uses its own voice")
print("\nIf any EMBED line says FELL BACK, that embed var is missing/misspelled in .env.")
print("A fallback key against the embed endpoint is the usual cause of the 401.")