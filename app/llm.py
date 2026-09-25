"""Optional: let Claude condense the report into plain-language short-video copy.

Used only when ANTHROPIC_API_KEY is set (or USE_LLM=1 with other Anthropic credentials).
It helps most with reports that don't follow the standard layout, or where the
rule-based extractive summary sounds stiff. Any failure falls back to the
rule-based parser, so uploads never break because of the LLM.
"""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .parser import DEFAULT_DEADLINES, Finding, Report, stance_of

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")

SYSTEM = """You turn internal audit reports into scripts for 45-60 second vertical videos \
(like YouTube Shorts) for busy executives who will not read the report.

Rules:
- Stay strictly faithful to the report. Never invent numbers, owners, dates or opinions.
- Plain, active language. Short sentences. No jargon without a quick explanation.
- Put the single most telling number from the report first in `condition` when one exists.
- Word limits: condition <= 40 words, risk <= 30, each recommendation bullet <= 18, response <= 30.
- risk_level: use the report's own rating. If it rates findings by priority, map 1=High, 2=Moderate, 3=Low. \
If there is no rating at all, use "Unrated".
- deadline_days: the remediation deadline the report sets for that rating, else null.
- response_stance: how management responded to the finding."""


class LLMFinding(BaseModel):
    number: int
    title: str = Field(description="Short headline, max 8 words, Title Case")
    risk_level: Literal["High", "Moderate", "Low", "Unrated"]
    condition: str
    risk: str
    recommendation_bullets: list[str]
    response: str
    response_stance: Literal["Agrees", "Agrees with reservations", "Disagrees", "Not provided"]
    deadline_days: int | None


class LLMReport(BaseModel):
    title: str = Field(description="Audited area, e.g. 'Accounts Payable'")
    reference: str
    date: str
    sender: str = Field(description="Who issued the report")
    objectives: str = Field(description="One sentence, <= 28 words")
    opinion: str = Field(description="Overall conclusion, <= 34 words")
    findings: list[LLMFinding]


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) or os.environ.get("USE_LLM") == "1"


def condense(report_text: str) -> Report:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"<report>\n{report_text}\n</report>\n\nWrite the video copy for every finding."}],
        output_format=LLMReport,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Claude did not return a script (stop_reason={response.stop_reason})")
    data: LLMReport = response.parsed_output

    report = Report(title=data.title, reference=data.reference, date=data.date, sender=data.sender,
                    objectives=data.objectives, opinion=data.opinion)
    for f in data.findings:
        report.findings.append(Finding(
            number=f.number, title=f.title, risk_level=f.risk_level, condition=f.condition, risk=f.risk,
            recommendation=" ".join(b.rstrip(".") + "." for b in f.recommendation_bullets),
            response=f.response,
            response_stance=f.response_stance if f.response else stance_of(f.response),
            deadline_days=f.deadline_days or DEFAULT_DEADLINES.get(f.risk_level),
        ))
    return report
