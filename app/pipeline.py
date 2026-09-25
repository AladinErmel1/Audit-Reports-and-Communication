"""Upload → text → findings → storyboards → narrated MP4 shorts.

State lives on disk under DATA_DIR/<report_id>/ so the server can restart
without losing reports:
    source.pdf     the uploaded report
    report.json    parsed report, storyboards and render status (served to the player)
    shorts/*.mp4   rendered videos, shorts/*.jpg posters
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import uuid
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from . import llm
from .parser import parse_report
from .pdf_text import extract_text
from .render import render_short
from .storyboard import build_shorts
from .tts import Narrator

log = logging.getLogger("audit_shorts")
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
WORKERS = int(os.environ.get("RENDER_WORKERS", max(1, min(4, os.cpu_count() or 2))))
_lock = threading.RLock()  # guards report.json writes and shared state updates


def report_dir(report_id: str) -> Path:
    return DATA_DIR / report_id


def load_state(report_id: str) -> dict | None:
    p = report_dir(report_id) / "report.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_state(report_id: str, state: dict) -> None:
    p = report_dir(report_id) / "report.json"
    tmp = p.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(p)


def list_reports() -> list[dict]:
    out = []
    if not DATA_DIR.exists():
        return out
    for d in DATA_DIR.iterdir():
        state = load_state(d.name) if d.is_dir() else None
        if state:
            out.append({k: state.get(k) for k in ("id", "filename", "status", "created", "title", "reference",
                                                  "date", "finding_count", "progress")})
    return sorted(out, key=lambda r: r.get("created") or 0, reverse=True)


def create_report(filename: str, data: bytes) -> str:
    report_id = uuid.uuid4().hex[:10]
    d = report_dir(report_id)
    (d / "shorts").mkdir(parents=True)
    (d / "source.pdf").write_bytes(data)
    save_state(report_id, {"id": report_id, "filename": filename, "status": "queued", "stage": "Queued",
                           "created": time.time(), "progress": 0, "shorts": []})
    return report_id


def process_report(report_id: str) -> None:
    """Runs in a background thread. Updates report.json as each short finishes."""
    state = load_state(report_id) or {}
    d = report_dir(report_id)

    def update(**kw) -> None:
        with _lock:
            state.update(kw)
            save_state(report_id, state)

    try:
        update(status="processing", stage="Reading the PDF", progress=3)
        text = extract_text(d / "source.pdf")
        if len(text.strip()) < 200:
            raise ValueError("No readable text in this PDF. Scanned reports need OCR first (e.g. `ocrmypdf`).")

        update(stage="Finding the findings", progress=8)
        parsed = parse_report(text)
        report, method = parsed, "rules"
        if llm.enabled():
            update(stage="Writing scripts with Claude", progress=10)
            try:
                report, method = llm.condense(text), "claude"
            except Exception as exc:  # noqa: BLE001
                log.warning("Claude condensing failed, using rule-based scripts: %s", exc)
        if not report.findings:
            raise ValueError("No findings were recognised. The parser looks for headings like "
                             "'FINDING #1 - Title:' followed by RISK / RECOMMENDATION / MANAGEMENT RESPONSE sections.")

        shorts = build_shorts(report)
        # The player's "Read the source" panel always shows the report's own wording.
        originals = {f.number: f for f in parsed.findings}
        for s in shorts:
            o = originals.get(s.finding_number) if s.finding_number else None
            if o:
                s.source = {"Finding": o.condition, "Risk / internal control": o.risk,
                            "Recommendation": o.recommendation, "Management response": o.response}
        label = " · ".join(x for x in [report.reference, report.title] if x) or "Audit report"
        entries = []
        for s in shorts:
            e = s.to_dict()
            e.update(status="queued", video=None, poster=None, duration=None)
            entries.append(e)
        update(stage="Rendering video shorts", progress=12, title=report.title, reference=report.reference,
               date=report.date, sender=report.sender, script_source=method, finding_count=len(report.findings),
               report=report.to_dict(), shorts=entries)

        # Frame compositing is CPU-bound Python, so shorts render in separate processes.
        # They are submitted in feed order, so the first shorts are watchable early.
        with ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = {pool.submit(_render_job, s, label, str(d)): i for i, s in enumerate(shorts)}
            for done, fut in enumerate(as_completed(futures), start=1):
                i = futures[fut]
                meta = fut.result()
                with _lock:
                    entries[i].update(status="ready", video=f"/media/{report_id}/shorts/{shorts[i].id}.mp4",
                                      poster=f"/media/{report_id}/shorts/{shorts[i].id}.jpg", **meta)
                    state["progress"] = 12 + int(88 * done / len(shorts))
                    save_state(report_id, state)
        update(status="ready", stage="Ready", progress=100)
    except Exception as exc:  # noqa: BLE001
        log.error("Processing %s failed: %s\n%s", report_id, exc, traceback.format_exc())
        update(status="error", stage="Failed", error=str(exc))


def _render_job(short, label: str, folder: str) -> dict:
    d = Path(folder)
    return render_short(short, label, d / "shorts" / f"{short.id}.mp4", d / "shorts" / f"{short.id}.jpg",
                        Narrator(), work_dir=d)


def start_processing(report_id: str) -> None:
    threading.Thread(target=process_report, args=(report_id,), daemon=True).start()
