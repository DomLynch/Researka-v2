REVIEWER_PROMPT_VERSION = "reviewer-v6-triage-anchors"
EDITOR_PROMPT_VERSION = "editor-v1-clean-runtime"


def render_prompt_context(domain_slug: str) -> dict[str, str]:
    return {
        "domain_slug": domain_slug,
        "reviewer_prompt_version": REVIEWER_PROMPT_VERSION,
        "editor_prompt_version": EDITOR_PROMPT_VERSION,
    }
