from __future__ import annotations

import re
from typing import Any

from contracts.templates import RAPID_EVIDENCE_SYNTHESIS

STRIP_LINE_PATTERNS = (
    re.compile(r"^.*coverage decay detected.*$", re.IGNORECASE),
    re.compile(r"^.*generate an updated rapid evidence synthesis.*$", re.IGNORECASE),
    re.compile(r"^.*replace this section.*$", re.IGNORECASE),
    re.compile(r"^.*the search summary is not a summary.*$", re.IGNORECASE),
    re.compile(r"^.*\bthe search summary is incomplete\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bthe key findings section is a placeholder\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bexpand the limitations section\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bexpand the key findings\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bdeterministic review records\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bleaves semantic support for llm review\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bbounded revise review outcome\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bthe proposed focus\b.*\bis not supported\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bthe proposal does not explain\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bfails to document\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bundermining transparency\b.*$", re.IGNORECASE),
    re.compile(r"^.*\beither revise the\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bis non-reproducible\b.*$", re.IGNORECASE),
    re.compile(r"^.*\breplace the current text\b.*$", re.IGNORECASE),
    re.compile(r"^\s*[-*•]\s*\w[\w-]*\s+the\s+(search|key|limitations|methods|evidence|proposal)\b.*$", re.IGNORECASE),
)
STRIP_SECTION_HEADINGS = ("Revision Brief", "Reviewer Notes")
REJECT_TOKENS = ("[placeholder", "[tbd", "[tk]", "[fill ")
LEDGER_BAD_TOKENS = (
    "coverage decay detected",
    "generate an updated rapid evidence synthesis",
    "no recent publications",
    "counterevidence",
    "mixed evidence",
    "replace this section",
    "revision brief",
    "search summary is incomplete",
    "key findings section is a placeholder",
    "expand the limitations section",
    "expand the key findings",
    "deterministic review records",
    "leaves semantic support for llm review",
    "bounded revise review outcome",
    "the proposed focus",
    "the proposal does not explain",
    "fails to document",
    "undermining transparency",
    "either revise the",
    "is non-reproducible",
    "replace the current",
    "is incomplete and",
    "is a placeholder",
)
REQUIRED_RAPID_SECTIONS = RAPID_EVIDENCE_SYNTHESIS.required_sections
MIN_SECTION_CHARS = 120


def contains_pipeline_leakage(body: str) -> bool:
    cleaned = str(body or "")
    if any(extract_markdown_section(cleaned, heading) for heading in STRIP_SECTION_HEADINGS):
        return True
    lowered = cleaned.lower()
    if any(token in lowered for token in REJECT_TOKENS):
        return True
    return any(pattern.match(line.strip()) for line in cleaned.splitlines() for pattern in STRIP_LINE_PATTERNS)


def sanitize_publication_body(body: str) -> tuple[str, list[str]]:
    actions: list[str] = []
    kept_lines: list[str] = []
    for line in str(body or "").splitlines():
        stripped = line.strip()
        matched = next((pattern for pattern in STRIP_LINE_PATTERNS if pattern.match(stripped)), None)
        if matched:
            actions.append(f"stripped_line:{matched.pattern}")
            continue
        kept_lines.append(line)
    cleaned = "\n".join(kept_lines)
    for heading in STRIP_SECTION_HEADINGS:
        if extract_markdown_section(cleaned, heading):
            cleaned = remove_markdown_section(cleaned, heading)
            actions.append(f"stripped_section:{heading}")
    lowered = cleaned.lower()
    for token in REJECT_TOKENS:
        if token in lowered:
            raise ValueError(f"publication_sanitizer: unresolved placeholder token '{token}'")
    return _normalize_spacing(cleaned), actions


def validate_rapid_structure(body: str) -> None:
    for heading in REQUIRED_RAPID_SECTIONS:
        section = extract_markdown_section(body, heading)
        visible = _visible_section_chars(section)
        if visible < MIN_SECTION_CHARS:
            raise ValueError(f"structure_gate: '{heading}' empty or placeholder-thin ({visible} chars)")


def sanitize_source_ledger(
    source_ledger: dict[str, Any] | None,
    *,
    fallback_objective: str = "",
) -> tuple[dict[str, Any], list[str]]:
    ledger = dict(source_ledger or {})
    actions: list[str] = []

    def clean_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = " ".join(value.split()).strip()
        if not text:
            return None
        lowered = text.lower()
        if any(token in lowered for token in LEDGER_BAD_TOKENS):
            return None
        return text

    def clean_list(key: str) -> list[str]:
        raw_items = ledger.get(key)
        if not isinstance(raw_items, list):
            return []
        cleaned_items: list[str] = []
        dropped = 0
        seen: set[str] = set()
        for item in raw_items:
            cleaned = clean_string(item)
            if not cleaned:
                dropped += 1
                continue
            normalized = cleaned.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned_items.append(cleaned)
        if dropped:
            actions.append(f"{key}:dropped_{dropped}_polluted_entries")
        return cleaned_items

    primary_query = clean_string(ledger.get("primary_query"))
    fallback = " ".join(str(fallback_objective or "").split()).strip()
    if not primary_query and fallback:
        primary_query = fallback
        actions.append("primary_query:replaced_with_objective")
    ledger["primary_query"] = primary_query or ""

    for key in ("fallback_queries", "retry_queries", "contradiction_queries", "revision_hints"):
        ledger[key] = clean_list(key)

    return ledger, actions


def extract_markdown_section(body: str, heading: str) -> str:
    pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)")
    match = pattern.search(str(body or ""))
    if not match:
        return ""
    return match.group(1).strip()


def remove_markdown_section(body: str, heading: str) -> str:
    pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\s*\n.*?(?=^## |\Z)")
    return _normalize_spacing(pattern.sub("", str(body or "")))


def _normalize_spacing(text: str) -> str:
    collapsed = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    return collapsed.strip() + "\n"


def _visible_section_chars(section: str) -> int:
    cleaned = re.sub(r"(?m)^\s*[-*]\s*", "", str(section or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return len(cleaned)
