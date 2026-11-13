"""Built-in song collections in the card: the list is well-formed, the card is in sync with it,
and the card's filter shows the right songs per style and language."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "song_collections.json"
CARD = ROOT / "custom_components" / "cuboai" / "www" / "cuboai-card.js"
STYLES = {"sleep", "morning", "baby", "calm", "noise"}
SUNG = {"sleep", "morning", "baby"}
LANGS = {"he", "en", "ru"}


def _songs():
    return json.loads(SRC.read_text(encoding="utf-8"))["songs"]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_song_collections", ROOT / "tools" / "gen_song_collections.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_entry_is_complete_and_unique():
    songs = _songs()
    ids = [s["id"] for s in songs]
    assert len(ids) == len(set(ids)), "a video appears twice"
    for s in songs:
        assert re.fullmatch(r"[A-Za-z0-9_-]{11}", s["id"]), s
        assert s["category"] in STYLES, s
        assert s["lang"] in LANGS | {"any"}, s
        assert s["name"].strip() and s["title"].strip() and s["channel"].strip(), s
        assert isinstance(s["seconds"], int) and 30 <= s["seconds"] <= 4 * 3600, s
        # sung songs are single songs, instrumental ones carry no language
        assert (s["lang"] == "any") == (s["category"] not in SUNG), s
        if s["category"] in SUNG:
            assert s["seconds"] <= 15 * 60, s


def test_each_language_has_every_sung_style_and_there_are_five_styles():
    songs = _songs()
    assert {s["category"] for s in songs} == STYLES
    for lang in LANGS:
        for style in SUNG:
            n = sum(1 for s in songs if s["lang"] == lang and s["category"] == style)
            assert n >= 4, f"{lang}/{style} has only {n} songs"
    for style in STYLES - SUNG:
        assert sum(1 for s in songs if s["category"] == style) >= 4, style


def test_card_is_in_sync_with_the_list():
    gen = _gen()
    raw = CARD.read_bytes().decode("utf-8")
    assert gen.build(raw, _songs()) == raw, "run python tools/gen_song_collections.py"


def test_collections_option_and_section_are_wired():
    src = CARD.read_text(encoding="utf-8")
    assert '<option value="collections">' in src
    assert "lullabiesHtml + collectionsHtml + customSongsHtml" in src
    assert 'filter === "collections") matchesCategory = false' in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_filter_by_style_language_and_search_in_node():
    src = CARD.read_text(encoding="utf-8")
    start = src.index("// BEGIN CUBOAI_SONG_COLLECTIONS")
    end = src.index("function cuboaiFindCameraState")
    block = src[start:end]
    js = (
        block
        + """
const L = [
  {lang:'he', category:'sleep', name:'נומי נומי', id:'aaaaaaaaaaa', seconds:120},
  {lang:'en', category:'sleep', name:'Twinkle', id:'bbbbbbbbbbb', seconds:120},
  {lang:'ru', category:'morning', name:'Зарядка', id:'ccccccccccc', seconds:120},
  {lang:'any', category:'sleep', name:'Music box', id:'ddddddddddd', seconds:1200},
];
const names = (a) => a.map(s => s.name).join('|');
console.log(JSON.stringify({
  heSleep: names(cuboaiCollectionSongs(L, 'sleep', 'he', '')),
  allSleep: names(cuboaiCollectionSongs(L, 'sleep', 'all', '')),
  ruSleep: names(cuboaiCollectionSongs(L, 'sleep', 'ru', '')),
  search: names(cuboaiCollectionSongs(L, 'sleep', 'all', 'TWINK')),
  ruMorning: names(cuboaiCollectionSongs(L, 'morning', 'ru', '')),
  uiHe: cuboaiUiLang({locale: {language: 'he'}}),
  uiRu: cuboaiUiLang({language: 'ru-RU'}),
  uiEn: cuboaiUiLang({locale: {language: 'de'}}),
  url: cuboaiYoutubeUrl('aaaaaaaaaaa'),
  styles: CUBOAI_SONG_STYLES.length,
}));
const cfg = (c) => cuboaiCollectionConfig(c);
const vis = (c, style, lang) => names(cuboaiVisibleSongs(L, cfg(c), style, lang, ''));
console.log(JSON.stringify({
  dflt: cfg(undefined),
  hidden: cfg({show_song_collections: false}).show,
  hiddenStr: cfg({show_song_collections: 'false'}).show,
  noStyles: cfg({song_styles: []}).show,
  onlyHe: cfg({song_languages: ['he']}),
  csv: cfg({song_languages: 'ru, EN', song_styles: 'calm,bogus,sleep'}),
  defaults: cfg({default_song_style: 'noise', default_song_language: 'ru'}),
  badDefaults: cfg({song_styles: ['calm'], default_song_style: 'sleep', default_song_language: 'xx'}),
  visHeAll: vis({song_languages: ['he']}, 'sleep', 'all'),
  visNoLangs: vis({song_languages: []}, 'sleep', 'all'),
  visStyleOff: vis({song_styles: ['morning']}, 'sleep', 'all'),
  visHidden: vis({show_song_collections: false}, 'sleep', 'all'),
}));
"""
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, encoding="utf-8", check=True)
    first, second = out.stdout.strip().splitlines()
    c = json.loads(second)
    assert c["dflt"] == {
        "show": True,
        "styles": ["sleep", "morning", "baby", "calm", "noise"],
        "langs": ["he", "en", "ru"],
        "defaultStyle": "sleep",
        "defaultLang": "all",
    }
    assert c["hidden"] is False and c["hiddenStr"] is False and c["noStyles"] is False
    assert c["onlyHe"]["langs"] == ["he"] and c["onlyHe"]["defaultLang"] == "he"
    assert c["csv"]["langs"] == ["en", "ru"] and c["csv"]["styles"] == ["sleep", "calm"]
    assert (c["defaults"]["defaultStyle"], c["defaults"]["defaultLang"]) == ("noise", "ru")
    assert (c["badDefaults"]["defaultStyle"], c["badDefaults"]["defaultLang"]) == ("calm", "all")
    assert c["visHeAll"] == "נומי נומי|Music box"  # other languages hidden, instrumental stays
    assert c["visNoLangs"] == "Music box"
    assert c["visStyleOff"] == "" and c["visHidden"] == ""
    r = json.loads(first)
    assert r["heSleep"] == "נומי נומי|Music box"
    assert r["allSleep"] == "נומי נומי|Twinkle|Music box"
    assert r["ruSleep"] == "Music box"
    assert r["search"] == "Twinkle"
    assert r["ruMorning"] == "Зарядка"
    assert (r["uiHe"], r["uiRu"], r["uiEn"]) == ("he", "ru", "en")
    assert r["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert r["styles"] == 5
