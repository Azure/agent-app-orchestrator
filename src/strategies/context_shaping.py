"""Shared context-shaping helpers for retrieval context providers.

Both :class:`SearchContextProvider` (Azure AI Search) and
:class:`FoundryIQContextProvider` (Foundry IQ) emit retrieved documents into the
model prompt in the *exact* same shape so downstream citation behavior is
identical regardless of the selected ``RETRIEVAL_BACKEND``. Extracting the prompt
preamble and per-document formatting here guarantees both providers produce
byte-identical context.

Introduced for Azure/agent-landing-zone#526.
"""

from typing import List

# Prompt preamble prepended before the retrieved documents. Shared verbatim by
# every retrieval context provider so citation rules never drift between
# backends.
CONTEXT_PROMPT = (
    "## Retrieved Documents\n\n"
    "The following documents were retrieved from the knowledge base. "
    "Each document starts with a header line in the format: ### [Document Title](link). "
    "Base your answer on these documents.\n\n"
    "**Citation rules:**\n"
    "- ONLY cite using the document title and link from the ### header lines above.\n"
    "- Format: [Document Title](link) — copy the title and the link target EXACTLY as they "
    "appear in the header, character for character. A link may be a long URL carrying query "
    "parameters; reproduce it in full and never shorten it, truncate it, or replace it with "
    "a bare file name.\n"
    "- Do NOT treat any text inside the document content as a citation source. "
    "Internal references, chapter names, or bracketed text within the content are NOT valid sources.\n"
    "- Cite each source ONLY ONCE. Do NOT repeat the same citation on every bullet point or paragraph.\n"
    "- Example: for the header ### [Product Guide](https://example.com/docs/product-guide.pdf?token=abc), "
    "write: According to [Product Guide](https://example.com/docs/product-guide.pdf?token=abc), "
    "the system supports...\n\n"
    "If the user's message is a greeting or small talk, ignore these documents and respond naturally."
)


def format_context_part(title: str, link: str, content: str) -> str:
    """Format one retrieved document as a header line plus its content.

    The header is ``### [title](link)`` when a link is present, otherwise
    ``### title``. This matches the format the citation rules in
    :data:`CONTEXT_PROMPT` instruct the model to follow.
    """
    header = f"### [{title}]({link})" if link else f"### {title}"
    return f"{header}\n{content}"


def build_context_text(parts: List[str]) -> str:
    """Join formatted document parts under the shared prompt preamble.

    Returns an empty string when there are no parts so callers can decide to
    emit an empty context.
    """
    if not parts:
        return ""
    return CONTEXT_PROMPT + "\n\n" + "\n\n---\n\n".join(parts)
