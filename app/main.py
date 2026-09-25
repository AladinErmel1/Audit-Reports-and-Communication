"""Audit Shorts web server: upload a report, watch its findings as short videos."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline

logging.basicConfig(level=logging.INFO)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_UPLOAD_MB = 60

app = FastAPI(title="Audit Shorts")
pipeline.DATA_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/reports", status_code=201)
async def upload_report(file: UploadFile) -> dict:
    name = file.filename or "report.pdf"
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF audit report.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"The PDF is larger than {MAX_UPLOAD_MB} MB.")
    report_id = pipeline.create_report(name, data)
    pipeline.start_processing(report_id)
    return {"id": report_id}


@app.get("/api/reports")
def list_reports() -> list[dict]:
    return pipeline.list_reports()


@app.get("/api/reports/{report_id}")
def get_report(report_id: str) -> dict:
    state = pipeline.load_state(_safe_id(report_id))
    if not state:
        raise HTTPException(404, "Report not found")
    return state


@app.delete("/api/reports/{report_id}", status_code=204)
def delete_report(report_id: str) -> None:
    d = pipeline.report_dir(_safe_id(report_id))
    if not d.exists():
        raise HTTPException(404, "Report not found")
    shutil.rmtree(d)


@app.get("/media/{report_id}/shorts/{name}")
def media(report_id: str, name: str) -> FileResponse:
    if "/" in name or ".." in name or not name.endswith((".mp4", ".jpg")):
        raise HTTPException(404)
    path = pipeline.report_dir(_safe_id(report_id)) / "shorts" / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4" if name.endswith(".mp4") else "image/jpeg")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg")),
            "espeak": bool(shutil.which("espeak-ng") or shutil.which("espeak"))}


def _safe_id(report_id: str) -> str:
    if not report_id.isalnum():
        raise HTTPException(404, "Report not found")
    return report_id


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
