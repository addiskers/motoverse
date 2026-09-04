"""
One-off: recompute `language` for every stored call using the current detection
logic in recorder.py (looks at ALL customer turns, recognises more Indian
scripts, and records "no_speech" when the customer never spoke).

Run from the project directory, then restart the app so its in-memory index
reloads the updated files:

    .venv/bin/python reindex_language.py
    sudo systemctl restart aicalling
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

import store  # noqa: E402  (needs DATA_DIR from .env)
from recorder import infer_language_from_turns  # noqa: E402


def main():
    calls_dir = store.CALLS_DIR
    if not os.path.isdir(calls_dir):
        print(f"No calls directory at {calls_dir}")
        return

    total = changed = 0
    counts = {}
    for name in sorted(os.listdir(calls_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(calls_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                call = json.load(f)
        except Exception as e:
            print(f"skip {name}: {e}")
            continue

        total += 1
        user_texts = [m.get("text") for m in (call.get("transcript") or []) if m.get("role") == "user"]
        new = infer_language_from_turns(user_texts)
        if new is None and call.get("status") != "in_progress":
            new = "no_speech"
        old = call.get("language")
        counts[new] = counts.get(new, 0) + 1

        if new != old:
            call["language"] = new
            store._save_sync(call)
            changed += 1
            print(f"{call.get('id')}: {old!r} -> {new!r}")

    print(f"\n{total} calls scanned, {changed} updated")
    print("language breakdown now:",
          dict(sorted(counts.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
