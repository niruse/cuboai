# Docs images

The screenshots in the main README are **rendered, not captured**: the real
`cuboai-card.js` (camera card, timeline card, card editor) runs on a local page
with a fake Home Assistant holding made-up sample data, and the setup /
Configure dialogs are drawn from the real config flow. No personal data can end
up in them, and they can be regenerated whenever the UI changes.

```bash
python tools/docs-images/dump_forms.py   # the real setup + Configure fields -> forms.json
node tools/docs-images/capture.mjs       # every shot -> docs/images/<name>.png (2x)
node tools/docs-images/capture.mjs card-live timeline-card   # just these
```

Needs Python with the test requirements (`requirements-test.txt`) and Node 22+
with Chrome or Edge installed (`CHROME=/path/to/chrome` to pick one). The page
loads Material Design icons from cdn.jsdelivr.net and the Roboto font from
Google Fonts.

| File | What it is |
|---|---|
| `dump_forms.py` | Builds every setup step and Configure through the real config flow (with the test suite's HA stubs) and writes `forms.json`: fields, order, types, defaults, ranges, labels and help text. |
| `harness.html`, `main.js` | One scene per `?shot=<name>`: the sample `hass`, the dialog / entities / notification renderers, and the card scenes. The clock is frozen at 2026-09-20 07:30 so every render shows the same night. |
| `stubs.js` | Stand-ins for `ha-card`, `ha-icon`, and the WebRTC Camera card (a drawn night-vision crib instead of video). |
| `capture.mjs` | Serves the repo, opens each shot in headless Chrome and saves a 2x PNG clipped to the scene. |

To preview a scene, serve the repo root (`python -m http.server 8765`) and open
`http://localhost:8765/tools/docs-images/harness.html?shot=card-live`.
