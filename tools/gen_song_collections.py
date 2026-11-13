"""Embed tools/song_collections.json into the card as CUBOAI_SONG_COLLECTIONS.

The JSON is the source of truth: every entry carries the YouTube id plus the title, channel and
length that were read from the live video page when it was checked. Run after editing it:

    python tools/gen_song_collections.py          # rewrite the block in the card
    python tools/gen_song_collections.py --check  # exit 1 if the card is out of date
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "song_collections.json"
CARD = ROOT / "custom_components" / "cuboai" / "www" / "cuboai-card.js"
BEGIN = "// BEGIN CUBOAI_SONG_COLLECTIONS"
END = "// END CUBOAI_SONG_COLLECTIONS"
KEYS = ("lang", "category", "name", "id", "title", "seconds")


def render(songs: list[dict]) -> str:
    rows = [
        "  " + json.dumps({k: s[k] for k in KEYS}, ensure_ascii=False, separators=(", ", ": ")) + "," for s in songs
    ]
    return "\n".join([BEGIN, "const CUBOAI_SONG_COLLECTIONS = [", *rows, "];", END])


def build(card_text: str, songs: list[dict]) -> str:
    nl = "\r\n" if "\r\n" in card_text else "\n"
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if not pattern.search(card_text):
        raise SystemExit("markers not found in the card")
    return pattern.sub(lambda _m: render(songs).replace("\n", nl), card_text, count=1)


def main() -> int:
    songs = json.loads(SRC.read_text(encoding="utf-8"))["songs"]
    raw = CARD.read_bytes().decode("utf-8")
    new = build(raw, songs)
    if "--check" in sys.argv:
        if new != raw:
            print("cuboai-card.js is out of date: run python tools/gen_song_collections.py")
            return 1
        return 0
    CARD.write_bytes(new.encode("utf-8"))
    print(f"embedded {len(songs)} songs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
