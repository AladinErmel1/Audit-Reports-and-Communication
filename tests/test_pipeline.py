"""Tests against the sample report: NCCU Internal Audit, Accounts Payable (AUD24001)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.parser import parse_report, stance_of
from app.pdf_text import extract_text
from app.storyboard import build_shorts, find_stat, spoken, summarize, tidy

SAMPLE = Path(__file__).parent / "fixtures" / "accounts-payable-audit.pdf"


@pytest.fixture(scope="module")
def text() -> str:
    return extract_text(SAMPLE)


@pytest.fixture(scope="module")
def report(text):
    return parse_report(text)


def test_page_furniture_removed(text):
    assert "Public Records Act" not in text
    assert "FAX 919.530.7656" not in text
    # Headings that repeat on many pages must survive furniture removal.
    assert text.count("MANAGEMENT RESPONSE:") == 9


def test_memo_header(report):
    assert report.reference == "AUD24001"
    assert report.title == "Accounts Payable"
    assert report.date == "May 30, 2024"
    assert report.sender.startswith("Robert Gaines")


def test_all_findings_with_sections(report):
    assert [f.number for f in report.findings] == list(range(1, 10))
    assert report.findings[3].title == "Lack of Segregation of Duties within the Eagles' Purch System"
    for f in report.findings:
        assert f.condition and f.risk and f.recommendation and f.response, f.number
        assert not f.risk.startswith("INTERNAL CONTROL")
        assert f.risk_level == "High" and f.priority == 1
        assert f.deadline_days == 90  # "Priority 1 ... within 90 days of report issuance"


def test_section_boundaries(report):
    f7 = report.findings[6]
    assert "$140,307.55" in f7.condition
    assert f7.risk.startswith("When invoices are paid multiple times")
    assert f7.response == "Management concurs with the recommendation. Policies will be updated and training implemented."


def test_management_stance(report):
    stances = {f.number: f.response_stance for f in report.findings}
    assert stances[1] == "Agrees"
    assert stances[4] == "Agrees with reservations"  # concerned about adding Purchasing to the workflow
    assert stance_of("Management does not concur with the finding.") == "Disagrees"
    assert stance_of("") == "Not provided"


def test_key_numbers(report):
    stats = {f.number: (find_stat(f.condition) or ("", ""))[0] for f in report.findings}
    assert stats[5] == "32%"
    assert stats[6] == "$25,201,427"
    assert stats[7] == "$140,307.55"
    assert stats[8] == "25%"


def test_storyboards(report):
    shorts = build_shorts(report)
    assert len(shorts) == 11  # overview + 9 findings + action list
    assert [s.kind for s in shorts[:2]] == ["overview", "finding"] and shorts[-1].kind == "actions"
    for s in shorts:
        words = sum(len(sc.narration.split()) for sc in s.scenes)
        assert words <= 185, (s.id, words)  # about a minute of speech
        kinds = [sc.kind for sc in s.scenes]
        assert kinds[0] == "title" and kinds[-1] == "outro"
    f7 = shorts[7]
    labels = [sc.label for sc in f7.scenes]
    assert labels == ["FINDING 7 OF 9", "THE KEY NUMBER", "WHAT WE FOUND", "WHY IT MATTERS",
                      "WHAT NEEDS TO HAPPEN", "MANAGEMENT RESPONSE", "YOUR ACTION"]
    assert f7.scenes[1].stat == "$140,307.55"
    # Captions keep the report's own figures; only the voice gets the spoken form.
    assert "$140,307.55" in f7.scenes[1].narration


def test_text_helpers():
    assert tidy("It is recommended that management update the policy.") == "Management update the policy."
    concession = ("While it is safe to assume that the majority of invoices were, in fact, completed, "
                  "the ongoing practice significantly increases the financial risk.")
    assert tidy(concession).startswith("The ongoing practice")
    assert spoken("$140,307.55 across 32% of AP invoices") == "140,307 dollars and 55 cents across 32 percent of A P invoices"
    assert summarize("Short. " * 3, 20) == []  # fragments under four words are dropped


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_render_short(report, tmp_path):
    from app.render import render_short
    from app.tts import Narrator

    short = build_shorts(report)[7]
    short.scenes = short.scenes[:2]
    meta = render_short(short, "AUD24001 · Accounts Payable", tmp_path / "s.mp4", tmp_path / "s.jpg", Narrator())
    assert (tmp_path / "s.jpg").exists()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-of", "csv=p=0", str(tmp_path / "s.mp4")], capture_output=True, text=True).stdout
    assert "video,720,1280" in probe
    if meta["narration"] != "silent":
        assert "audio" in probe
    assert meta["duration"] > 3 and meta["captions"]
