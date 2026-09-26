"""The Claude path, exercised through the real SDK with a mocked HTTP transport (no key, no network)."""
from __future__ import annotations

import json

import anthropic
import httpx2 as httpx  # anthropic SDK 1.x is built on httpx2

from app import llm
from app.storyboard import build_shorts

REPLY = {
    "title": "Accounts Payable", "reference": "AUD24001", "date": "May 30, 2024",
    "sender": "Robert Gaines, Director of Internal Audit",
    "objectives": "We checked how invoices are processed and why payments are late.",
    "opinion": "Invoice processing needs several improvements to reach adequate internal control.",
    "findings": [{
        "number": 7, "title": "Duplicate Payments", "risk_level": "High",
        "condition": "We found $140,307.55 paid twice across 70 invoices. Cancelled invoices were relabelled inconsistently.",
        "risk": "Money paid twice is lost unless a vendor notices and refunds it.",
        "recommendation_bullets": ["Write and test procedures for all recurring AP tasks", "Train staff on them"],
        "response": "Management agrees. Policies will be updated and training implemented.",
        "response_stance": "Agrees", "deadline_days": 90,
    }],
}


def test_condense_request_and_mapping():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": sent["model"],
            "content": [{"type": "text", "text": json.dumps(REPLY)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    report = llm.condense("report text", client=client)

    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in sent  # Opus 5.5 always thinks; disabling it would be a 400

    f = report.findings[0]
    assert (f.number, f.risk_level, f.response_stance, f.deadline_days) == (7, "High", "Agrees", 90)
    short = build_shorts(report)[1]
    assert short.scenes[1].stat == "$140,307.55"
