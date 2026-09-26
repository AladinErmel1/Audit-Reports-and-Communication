# Audit Shorts

Upload an audit report (PDF) and get one short vertical video per finding, like YouTube Shorts or Instagram Reels.
Addressees watch the findings instead of reading the report.

Each report becomes:

| Short | What it shows |
|---|---|
| **The audit at a glance** | Audited area, objective, number of findings and their risk ratings, the auditor's opinion |
| **One short per finding** (45–60 s) | Finding title and risk rating → the key number (animated) → what we found → why it matters → what needs to happen → management's response (agrees / with reservations / disagrees) → deadline |
| **Your action list** | All actions grouped by deadline, plus who to contact |

Every short has narration, burned-in captions (so it works on mute) and story-style progress segments.

The player works like Shorts/Reels: swipe or scroll vertically, videos autoplay, and a tap turns on sound. The action rail has:
- **Got it**: the viewer acknowledges the finding (kept in their browser for now)
- **Details**: jump to a chapter, or read the report's original wording for that finding
- **Share**: copies a link to that exact short
- **Save**: downloads the MP4, for email, Teams or an intranet page

## Run it

Requirements: Python 3.10+, `ffmpeg`, and `espeak-ng` for offline narration.

```bash
# Debian/Ubuntu: sudo apt install ffmpeg espeak-ng     macOS: brew install ffmpeg espeak-ng
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and drop a PDF. The first shorts are ready to watch within a minute; the whole
sample report (9 findings, 11 shorts) renders in about 3 minutes on a 4-core machine.

## Configuration (environment variables)

### Adding your Claude API key (without putting it on GitHub)

1. In the project folder, copy `.env.example` to `.env`.
2. Open `.env` and paste your key after `ANTHROPIC_API_KEY=`.
3. Restart the server. http://localhost:8000/api/health now shows `"claude_scripts": true`.

`.env` is listed in `.gitignore`, so git never commits or pushes it. Run `git status` and it won't appear.
On a hosting platform (Railway, Render, Azure and so on), don't upload a `.env` file. Add `ANTHROPIC_API_KEY` in the
platform's *environment variables / secrets* settings instead; the app reads it from there.
If a key is ever committed by mistake, revoke it in the Claude Console and create a new one.
Removing the file in a later commit is not enough, because it stays in the git history.

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Claude condenses each finding into plain-language copy (`app/llm.py`, model `claude-opus-5`, override with `CLAUDE_MODEL`). Without it a rule-based extractive summariser is used. If the Claude call fails, the app falls back to the rules. |
| `TTS_ENGINE` | `auto` (default: tries piper → edge → espeak), `piper`, `edge`, `espeak` or `silent` |
| `PIPER_VOICE` | Path to a Piper `.onnx` voice for natural offline narration (`pip install piper-tts`) |
| `EDGE_VOICE` | Microsoft neural voice for `edge-tts`, e.g. `en-US-AndrewNeural`, `en-GB-SoniaNeural`, `de-CH-LeniNeural` |
| `RENDER_WORKERS` | How many shorts render in parallel (default: CPU cores, max 4) |
| `SHORTS_FPS` | Frame rate, default 30 |
| `DATA_DIR` | Where uploads and videos are stored (default `./data`) |

## How it works

```
PDF ─► pdf_text.py   text, with repeated headers/footers removed
    ─► parser.py     memo header, objectives, opinion, findings
                     (condition / risk / recommendation / management response, priority → risk and deadline)
    ─► llm.py        optional: Claude rewrites the copy
    ─► storyboard.py scenes + narration per short (≈150 words ≈ 1 minute)
    ─► tts.py        narration audio
    ─► render.py     720×1280 frames (Pillow) piped to ffmpeg → H.264/AAC MP4 + poster
    ─► web/          library + vertical feed player (vanilla JS, no build step)
```

The parser handles the common IIA-style layout: `FINDING #n – Title:`, then `RISK / INTERNAL CONTROL:`,
`RECOMMENDATION:` and `MANAGEMENT RESPONSE:` blocks. It also accepts `OBSERVATION`/`ISSUE`, `CRITERIA`, `CAUSE`,
`CONDITION`, `IMPACT`/`EFFECT` and `MANAGEMENT ACTION PLAN`, and tolerates common OCR errors. Reports with a
different structure work best with `ANTHROPIC_API_KEY` set. Scanned PDFs without a text layer need OCR first
(for example `ocrmypdf`).

## Tests

```bash
pytest -q
```

The tests run against the sample report in `tests/fixtures/` (NCCU Internal Audit, *Accounts Payable*, AUD24001).
They cover text cleaning, the parsed findings, stances, key numbers, storyboards and an actual MP4 render.

## Ideas for next steps

- Viewer tracking on the server: who watched and acknowledged which finding, with reminders to open items
- Addressee-specific playlists (e.g. only the findings assigned to the CFO)
- Multilingual narration (German/French via `EDGE_VOICE` or Piper voices, plus translated copy via Claude)
- Corporate branding (logo, colours, intro/outro)
