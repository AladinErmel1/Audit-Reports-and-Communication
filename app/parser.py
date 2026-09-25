"""Rule-based parser for internal audit reports.

Works offline and without an LLM. It recognises the conventional layout used by
most internal audit reports (IIA style): a memo header, an executive summary,
objectives/scope, an opinion, and a list of findings, each with condition,
risk, recommendation and management response blocks.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Canonical section keys and the heading spellings (incl. common OCR errors) that map to them.
_FINDING_HEAD = re.compile(
    r"^(?:\d+\.\s*)?(?:FINDING|OBSERVATION|ISSUE)\s*(?:NO\.?|#)?\s*(\d+)\s*[-–—:.]?\s*(.*?):?\s*$",
    re.IGNORECASE,
)
_SUB_HEADS = [
    ("risk", re.compile(r"^(?:RIS(?:K|I<|\S)?\s*(?:/\s*INTERNAL\s+CONTROLS?)?|IMPACT|EFFECT|RISK\s+AND\s+IMPACT)\s*:\s*(.*)$", re.IGNORECASE)),
    ("recommendation", re.compile(r"^RECOMMENDATIONS?\s*:\s*(.*)$", re.IGNORECASE)),
    ("response", re.compile(r"^(?:MANAGEMENT(?:'S)?\s+RESPONSES?|MANAGEMENT\s+ACTION\s+PLAN|AUDITEE\s+RESPONSE)\s*:\s*(.*)$", re.IGNORECASE)),
    ("criteria", re.compile(r"^CRITERIA\s*:\s*(.*)$", re.IGNORECASE)),
    ("cause", re.compile(r"^(?:ROOT\s+)?CAUSE\s*:\s*(.*)$", re.IGNORECASE)),
    ("condition", re.compile(r"^CONDITION\s*:\s*(.*)$", re.IGNORECASE)),
]
_TOP_HEADS = {
    "executive_summary": r"EXECUTIVE\s+SUMMARY",
    "introduction": r"INTRODUCTION|BACKGROUND",
    "objectives": r"OBJECTIVES?",
    "scope": r"SCOPE",
    "methodology": r"METHODOLOGY",
    "opinion": r"(?:AUDITOR(?:'S)?\s+)?(?:OPINION|OVERALL\s+CONCLUSION|CONCLUSION)",
    "auditor_note": r"AUDITOR\s+NOTE",
    "acknowledgement": r"ACKNOWLEDGE?MENTS?",
}
_TOP_RE = [(k, re.compile(rf"^(?:{v})\s*:\s*(.*)$", re.IGNORECASE)) for k, v in _TOP_HEADS.items()]
_PRIORITY_HEAD = re.compile(r"PRIORITY\s*(\d)\s*FINDINGS?\s*(HIGH|MODERATE|MEDIUM|LOW)?", re.IGNORECASE)
_RISK_WORD = re.compile(r"\b(high|moderate|medium|low)\s+risk\b|\brisk\s*(?:rating|level)?\s*:\s*(high|moderate|medium|low)\b", re.IGNORECASE)
_MEMO_FIELDS = {"date": "DATE", "to": "TO", "from": "FROM", "subject": "SUBJECT", "cc": "CC"}

PRIORITY_TO_RISK = {1: "High", 2: "Moderate", 3: "Low"}
DEFAULT_DEADLINES = {"High": 90, "Moderate": 120, "Low": 180}


@dataclass
class Finding:
    number: int
    title: str
    risk_level: str = "Unrated"
    priority: int | None = None
    condition: str = ""
    criteria: str = ""
    cause: str = ""
    risk: str = ""
    recommendation: str = ""
    response: str = ""
    response_stance: str = "Not provided"
    deadline_days: int | None = None


@dataclass
class Report:
    title: str = "Audit Report"
    reference: str = ""
    auditor: str = ""
    date: str = ""
    to: str = ""
    sender: str = ""
    executive_summary: str = ""
    objectives: str = ""
    scope: str = ""
    opinion: str = ""
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _is_noise(line: str) -> bool:
    """Chart axis labels, legend entries and similar fragments carry no prose."""
    words = re.findall(r"[A-Za-z]{2,}", line)
    return len(words) < 3 and not line.rstrip().endswith((".", ":"))


def _join(lines: list[str]) -> str:
    text = " ".join(ln for ln in lines if not _is_noise(ln))
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _title_case(s: str) -> str:
    small = {"a", "an", "and", "the", "of", "or", "to", "in", "on", "for", "by", "at", "with", "without", "within", "than", "after", "not"}
    words = s.lower().split()
    return " ".join(w if (i and w in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))


def stance_of(response: str) -> str:
    r = response.lower()
    if not r.strip():
        return "Not provided"
    if re.search(r"\b(does not|do not|doesn't|don't) (concur|agree)|\bdisagrees?\b|\bnon-?concur", r):
        return "Disagrees"
    agrees = re.search(r"\b(concurs?|agrees?|accepts?)\b", r)
    reservations = re.search(r"\bhowever\b|\bconcerned\b|\bbut\b|\bexplanation\b|\bpartially\b|\bsuggests?\b", r)
    if agrees and reservations:
        return "Agrees with reservations"
    if agrees:
        return "Agrees"
    return "Response provided"


def _summary_titles(text: str) -> dict[int, str]:
    """Clean finding titles from an executive summary list ("FINDING #1: Outdated Policies.")."""
    titles = {}
    for m in re.finditer(r"FINDING\s*#\s*(\d+)\s*:\s*([^.]{3,120})\.", text, re.IGNORECASE):
        titles[int(m.group(1))] = " ".join(m.group(2).split())
    return titles


def _memo(lines: list[str]) -> dict[str, str]:
    """Memo header fields: the label is on its own line, the value on the next line(s)."""
    out: dict[str, str] = {}
    for key, label in _MEMO_FIELDS.items():
        for i, ln in enumerate(lines[:80]):
            m = re.match(rf"^{label}\s*:\s*(.*)$", ln, re.IGNORECASE)
            if m:
                value = m.group(1).strip() or (lines[i + 1] if i + 1 < len(lines) else "")
                out[key] = value.strip()
                break
    return out


def parse_report(text: str, deadlines: dict[str, int] | None = None) -> Report:
    lines = text.splitlines()
    report = Report()
    memo = _memo(lines)
    report.date = memo.get("date", "")
    report.to = memo.get("to", "")
    report.sender = re.sub(r"[^\w\s,.&'-]+.*$", "", memo.get("from", "")).strip(" ,")
    subject = memo.get("subject", "")
    ref = re.match(r"^\s*([A-Z]{2,5}\s*\d[\dO]{2,})\s*[-–:]\s*(.+)$", subject)
    if ref:
        report.reference = ref.group(1).replace("O", "0")
        report.title = ref.group(2).strip()
    elif subject:
        report.title = subject
    report.auditor = _guess_auditor(text)

    top: dict[str, list[str]] = {}
    findings: list[Finding] = []
    current_top: str | None = None
    current: Finding | None = None
    current_sub: str | None = None
    buf: dict[str, list[str]] = {}
    priority: int | None = None
    level_hint: str | None = None

    def flush() -> None:
        if current is not None:
            for k, v in buf.items():
                setattr(current, k, _join(v))

    for ln in lines:
        pm = _PRIORITY_HEAD.search(ln)
        if pm:
            priority = int(pm.group(1))
            level_hint = (pm.group(2) or "").title() or None
            continue

        fm = _FINDING_HEAD.match(ln)
        if fm and (ln.isupper() or ln.rstrip().endswith(":")) and not _summary_titles(ln):
            flush()
            num = int(fm.group(1))
            current = Finding(number=num, title=_title_case(fm.group(2).strip(" -–:")) or f"Finding {num}")
            if priority:
                current.priority = priority
                current.risk_level = level_hint or PRIORITY_TO_RISK.get(priority, "Unrated")
            findings.append(current)
            current_sub, current_top = "condition", None
            buf = {"condition": []}
            continue

        top_hit = next(((k, m) for k, rx in _TOP_RE if (m := rx.match(ln))), None)
        if top_hit and ln.split(":")[0].isupper():
            flush()
            current, current_sub = None, None
            buf = {}
            current_top = top_hit[0]
            top.setdefault(current_top, [])
            if top_hit[1].group(1):
                top[current_top].append(top_hit[1].group(1))
            continue

        if current is not None:
            sub_hit = next(((k, m) for k, rx in _SUB_HEADS if (m := rx.match(ln))), None)
            if sub_hit and ln.split(":")[0].replace("/", "").replace(" ", "").isupper():
                current_sub = sub_hit[0]
                buf.setdefault(current_sub, [])
                if sub_hit[1].group(1):
                    buf[current_sub].append(sub_hit[1].group(1))
                continue
            buf.setdefault(current_sub or "condition", []).append(ln)
            rm = _RISK_WORD.search(ln)
            if rm and current.risk_level == "Unrated":
                current.risk_level = (rm.group(1) or rm.group(2)).title().replace("Medium", "Moderate")
        elif current_top:
            top[current_top].append(ln)
    flush()

    report.executive_summary = _join(top.get("executive_summary", []))
    report.objectives = _join(top.get("objectives", []))
    report.scope = _join(top.get("scope", []))
    report.opinion = _join(top.get("opinion", []))

    # Prefer the cleaner titles from the executive summary; OCR mangles upper-case headings.
    titles = _summary_titles(report.executive_summary)
    deadlines = deadlines or _deadlines(text)
    seen: set[int] = set()
    for f in findings:
        if f.number in seen:
            continue
        seen.add(f.number)
        if f.number in titles:
            f.title = titles[f.number]
        f.response_stance = stance_of(f.response)
        f.deadline_days = deadlines.get(f.risk_level)
        report.findings.append(f)
    return report


def _deadlines(text: str) -> dict[str, int]:
    """Remediation deadlines from a "Priority N ... within X days" definitions section."""
    found = dict(DEFAULT_DEADLINES)
    for m in re.finditer(r"Priority\s*(\d)\s+(?:issues|recommendations).{0,900}?within\s+(\d{2,3})\s+days", text, re.IGNORECASE | re.DOTALL):
        level = PRIORITY_TO_RISK.get(int(m.group(1)))
        if level:
            found[level] = int(m.group(2))
    return found


def _guess_auditor(text: str) -> str:
    m = re.search(r"\b((?:Group\s+)?Internal\s+Audit(?:\s+(?:Office|Department|Function|Division|Services))?)\b", text[:4000], re.IGNORECASE)
    return " ".join(m.group(1).split()) if m else ""
