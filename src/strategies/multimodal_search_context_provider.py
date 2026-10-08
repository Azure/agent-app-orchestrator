"""Multimodal Search Context Provider.

Extends search functionality to retrieve both text documents and related
images from Azure AI Search.  Images (stored in Azure Blob Storage by the
ingestion pipeline) are downloaded as base64 and encoded in a structured
JSON format within ``ChatMessage.text``.  The companion
``MultimodalChatClient`` detects this encoding and converts it to OpenAI's
vision content array.

Index fields used (set by agent-app-ingestion multimodal chunker):
- contentVector   — text embedding
- captionVector   — image-caption embedding
- relatedImages   — list of blob URLs for figures
- imageCaptions   — concatenated caption text for all figures in the chunk
"""

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, MutableSequence
from typing import Any, Optional
from urllib.parse import urlparse

from agent_framework import ChatMessage, Context, ContextProvider, Role
from azure.core.credentials_async import AsyncTokenCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery, QueryType, QueryCaptionType
from azure.storage.blob.aio import BlobClient as AzureBlobClient

from connectors.multimodal_chat_client import MULTIMODAL_PREFIX
from connectors.search import _classify_retrieval_error, build_conversation_filter
from connectors.obo import RetrievalAuthorizationMode, require_retrieval_token
from dependencies import get_config
from telemetry import AuditEmitter, ReasonCode
from util.metadata import format_custom_metadata, parse_allowed_keys

logger = logging.getLogger(__name__)

_CONTEXT_PROMPT = (
    "## Retrieved Documents (with images)\n\n"
    "The following documents were retrieved from the knowledge base. "
    "Each document starts with a header line in the format: ### [Document Title](filepath). "
    "Some documents include images at their original positions within the text. "
    "Each image is preceded by a label: `📎 Image (embed once only): <image_path>`.\n\n"
    "Base your answer on these documents and images.\n\n"
    "**Citation rules:**\n"
    "- ONLY cite using the document title and filepath from the ### header lines above.\n"
    "- You MUST use this exact markdown format: [Document Title](filepath)\n"
    "- CORRECT: [Product Guide](product-guide.pdf) — WRONG: Product Guide [product-guide.pdf]\n"
    "- Do NOT treat any text inside the document content as a citation source.\n"
    "- Cite each source ONLY ONCE. Do NOT repeat the same citation on every bullet point or paragraph.\n"
    "- Example: According to [Product Guide](product-guide.pdf), the diagram shows...\n\n"
    "**Image embedding rules:**\n"
    "- Images appear at their ORIGINAL positions within the source text, preceded by a `📎 Image (embed once only): <path>` label.\n"
    "- BEFORE embedding any image, evaluate its visual content using your vision capabilities:\n"
    "  - INCLUDE if the image shows: mechanical parts, procedural diagrams, exploded views, measurements, tool usage, assembly steps, cross-sections, or anything directly relevant to the procedure.\n"
    "  - SKIP if the image is: a cartoon, humorous illustration, decorative artwork, filler drawing, logo, header/footer, splash/boot/system-output screen, or simply unrelated to the technical topic.\n"
    "- For images you INCLUDE, embed using markdown: ![Figure N](image_path) — use the figure number from the filename (e.g., `![Figure 92.1](path)`).\n"
    "- Use the EXACT image_path from the `📎 Image (embed once only):` label that precedes each image.\n"
    "- NEVER list image filenames as plain text — always use ![...](path) markdown.\n"
    "- Write a short introductory sentence before each image so the reader knows what it shows (e.g., 'The diagram below illustrates the rocker arm assembly:').\n"
    "- Place each included image INSIDE your answer, right after the step or paragraph it illustrates. NEVER collect images into a separate section at the end.\n"
    "- **STRICT DEDUPLICATION — CRITICAL**: Each image path must appear EXACTLY ONCE in your entire response. Even if the same `📎 Image:` label appears multiple times in the context, you MUST reference each path only once. Writing the same ![...](path) markdown a second time is strictly forbidden.\n\n"
    "If the user's message is a greeting or small talk, ignore these documents and respond naturally."
)

_FIGURE_RE = re.compile(r'\s*<figure>(.*?)</figure>\s*', re.DOTALL)

_CAPTION_ENTRY_RE = re.compile(r'\[([^\]]+)\]:\s*(.*?)(?=\[[^\]]+\]:|$)', re.DOTALL)
_TOKEN_RE = re.compile(r'[a-z0-9][a-z0-9_\-/\.]+', re.IGNORECASE)
_IRRELEVANT_IMAGE_HINTS = {
    "boot", "boots", "shoe", "shoes", "footwear", "cartoon", "decorative",
    "mascot", "logo", "watermark", "header", "footer", "divider", "splash",
    "screen", "dialog", "window", "warning", "error", "icon", "stamp",
    "duck", "frog", "humorous",
}
_PROCEDURAL_IMAGE_HINTS = {
    "adjust", "adjustment", "assembly", "bearing", "brake", "cam", "camshaft",
    "carb", "carburetor", "carburettor", "clearance", "clutch", "crank",
    "crankcase", "crankshaft", "cross-section", "cross", "cylinder", "diagram",
    "distributor", "engine", "exploded", "flywheel", "fuel", "gasket", "gear",
    "ignition", "install", "inspection", "measure", "measurement", "mechanism",
    "parts", "piston", "procedure", "pulley", "pushrod", "pushrods", "rebuild",
    "rocker", "shaft", "spec", "specs", "step", "timing", "tool", "torque",
    "valve", "valves",
}
_QUERY_STOPWORDS = {
    "a", "an", "and", "are", "do", "for", "from", "how", "i", "in", "is",
    "it", "of", "on", "or", "step", "steps", "the", "to", "vw", "you",
}


def _normalize_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def _extract_local_text(full_text: str, start: int, end: int, radius: int = 180) -> str:
    prefix = full_text[max(0, start - radius):start]
    suffix = full_text[end:min(len(full_text), end + radius)]
    return _normalize_whitespace(f"{prefix} {suffix}")


def _salient_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_RE.findall((text or "").lower())
        if len(token) >= 4 and token.lower() not in _QUERY_STOPWORDS
    }


def _parse_image_captions(captions_str: str, figure_paths: list[str]) -> dict[str, str]:
    """Parse imageCaptions into a best-effort mapping of figure path to caption."""
    if not captions_str.strip():
        return {}

    captions_by_path: dict[str, str] = {}
    ordered_captions: list[str] = []
    basenames = {path.split("/")[-1].lower(): path for path in figure_paths}

    for raw_key, raw_caption in _CAPTION_ENTRY_RE.findall(captions_str):
        caption = _normalize_whitespace(raw_caption)
        if not caption:
            continue

        key = raw_key.strip()
        key_basename = key.split("/")[-1].lower()
        matched_path = None
        if key in figure_paths:
            matched_path = key
        elif key_basename in basenames:
            matched_path = basenames[key_basename]

        if matched_path:
            captions_by_path[matched_path] = caption
        else:
            ordered_captions.append(caption)

    for path in figure_paths:
        if path not in captions_by_path and ordered_captions:
            captions_by_path[path] = ordered_captions.pop(0)

    return captions_by_path


def _is_relevant_figure(*, fig_path: str, caption: str, local_text: str, query: str) -> bool:
    """Return True only for figures that look procedurally relevant."""
    source_text = _normalize_whitespace(f"{fig_path} {caption}").lower()
    context_text = _normalize_whitespace(local_text).lower()
    query_tokens = _salient_tokens(query)

    source_negative = any(hint in source_text for hint in _IRRELEVANT_IMAGE_HINTS)
    context_negative = any(hint in context_text for hint in _IRRELEVANT_IMAGE_HINTS)
    source_positive = any(hint in source_text for hint in _PROCEDURAL_IMAGE_HINTS)
    context_positive = any(hint in context_text for hint in _PROCEDURAL_IMAGE_HINTS)
    query_positive = any(token in source_text or token in context_text for token in query_tokens)

    if source_negative and not source_positive:
        return False

    if caption:
        return source_positive or context_positive or query_positive

    if source_negative:
        return False
    if context_negative and not source_positive and not query_positive:
        return False
    return source_positive or context_positive or query_positive


def _build_image_hint(*, caption: str, local_text: str) -> str:
    if caption:
        return caption
    local_hint = _normalize_whitespace(local_text)
    if len(local_hint) > 180:
        return local_hint[:177].rstrip() + "..."
    return local_hint


def _extract_blob_relative_path(blob_url: str) -> str | None:
    """Extract the relative path from a full Azure Blob Storage URL.

    Given a URL like
    ``https://account.blob.core.windows.net/documents-images/path/fig.png``
    returns ``path/fig.png`` (everything after the container segment).
    Returns *None* if the URL cannot be parsed.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(blob_url)
        # path looks like "/container-name/path/to/image.png"
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2:
            return parts[1]  # everything after container
    except ValueError:
        pass
    return None


class MultimodalSearchContextProvider(ContextProvider):
    """Azure AI Search context provider with multimodal (text + image) support."""

    def __init__(
        self,
        *,
        endpoint: str,
        index_name: str,
        credential: AsyncTokenCredential,
        blob_credential: AsyncTokenCredential,
        conversation_id: Optional[str] = None,
        top_k: int = 3,
        max_images: int = 10,
        max_images_per_doc: int = 3,
        semantic_configuration_name: str | None = None,
        embed_fn: Callable[[str], Awaitable[list[float]]] | None = None,
        max_content_chars: int = 1500,
        get_obo_token: Callable[[], Awaitable[Optional[str]]] | None = None,
        authorization_mode: RetrievalAuthorizationMode = RetrievalAuthorizationMode.USER_REQUIRED,
        classify_images_fn: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
        classify_images_concurrency: int = 2,
        owner_id: Optional[str] = None,
    ) -> None:
        self._endpoint = endpoint
        self._index_name = index_name
        self._credential = credential
        self._blob_credential = blob_credential
        self._conversation_id = (conversation_id or "").strip() or None
        self._owner_id = (owner_id or "").strip() or None
        self._top_k = top_k
        self._max_images = max_images
        self._max_images_per_doc = max_images_per_doc
        self._semantic_config = semantic_configuration_name
        self._embed_fn = embed_fn
        self._max_content_chars = max_content_chars
        self._get_obo_token = get_obo_token
        self._authorization_mode = authorization_mode
        self._classify_images_fn = classify_images_fn
        self._classify_images_concurrency = max(1, classify_images_concurrency)
        # Populated during get_context — maps fig_path → base64 for post-response validation
        self.image_data: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def invoking(
        self,
        messages: ChatMessage | MutableSequence[ChatMessage],
        **kwargs: Any,
    ) -> Context:
        self.image_data.clear()
        msgs = [messages] if isinstance(messages, ChatMessage) else list(messages)
        user_texts = [
            m.text for m in msgs
            if m and m.text and m.text.strip() and m.role == Role.USER
        ]
        if not user_texts:
            return Context()

        query = user_texts[-1]
        search_start = time.time()
        logger.info(
            "[MultimodalSearchContextProvider] Query: %r (top_k=%d, hybrid=%s)",
            query[:120], self._top_k, bool(self._embed_fn),
        )

        # ---- Build search parameters ----
        # Custom metadata in context (default OFF). Only select the field when
        # enabled so pre-#487 indexes (which lack it) are not 400'd by Search.
        cfg = get_config()
        include_metadata = cfg.get("SEARCH_INCLUDE_METADATA_IN_CONTEXT", False, type=bool)
        metadata_max_chars = int(cfg.get("SEARCH_METADATA_MAX_CHARS", 500, type=int))
        metadata_allowed_keys = parse_allowed_keys(cfg.get("SEARCH_METADATA_ALLOWED_KEYS", "", type=str))

        select_fields = [
            "id", "content", "title", "filepath", "url",
            "relatedImages", "imageCaptions",
        ]
        if include_metadata:
            select_fields = select_fields + ["custom_metadata"]

        search_params: dict[str, Any] = {
            "search_text": query,
            "top": self._top_k,
            "select": select_fields,
        }

        # Conversation scoping: only this caller's chunks in this conversation
        # plus shared/global chunks.
        search_params["filter"] = build_conversation_filter(
            self._conversation_id, owner_id=self._owner_id, field_name="conversationId"
        )

        # Dual vector queries: contentVector + captionVector
        if self._embed_fn:
            try:
                embed_start = time.time()
                vector = await self._embed_fn(query)
                search_params["vector_queries"] = [
                    VectorizedQuery(
                        vector=vector,
                        k=self._top_k,
                        fields="contentVector",
                    ),
                    VectorizedQuery(
                        vector=vector,
                        k=self._top_k,
                        fields="captionVector",
                    ),
                ]
                logger.info(
                    "[MultimodalSearchContextProvider] Embedding generated in %.2fs (dims=%d)",
                    time.time() - embed_start, len(vector),
                )
            except Exception as e:
                logger.warning(
                    "[MultimodalSearchContextProvider] Embedding failed, falling back to keyword (%s)", type(e).__name__,
                )

        if self._semantic_config:
            search_params["query_type"] = QueryType.SEMANTIC
            search_params["semantic_configuration_name"] = self._semantic_config
            search_params["query_caption"] = QueryCaptionType.EXTRACTIVE

        # ---- Execute search ----
        try:
            obo_token: Optional[str] = None
            obo_token = await require_retrieval_token(self._authorization_mode, self._get_obo_token)

            if obo_token:
                search_params["x_ms_query_source_authorization"] = f"Bearer {obo_token}"

            async with SearchClient(
                endpoint=self._endpoint,
                index_name=self._index_name,
                credential=self._credential,
            ) as client:
                results = await client.search(**search_params)
                docs = []
                async for doc in results:
                    docs.append(doc)

        except Exception as e:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="azure_ai_search_multimodal",
                reason_code=ReasonCode.SOURCE_REJECTED,
            )
            level, marker = _classify_retrieval_error(e)
            logger.log(
                level, "%s Search failed: %s", marker, type(e).__name__,
                extra={"retrieval_credential_type": "obo" if obo_token else "managed_identity"},
            )
            raise

        logger.info(
            "[MultimodalSearchContextProvider] Search returned %d documents in %.2fs",
            len(docs), time.time() - search_start,
        )

        if not docs:
            AuditEmitter.default().emit_source(
                selected=False,
                source_type="azure_ai_search_multimodal",
                reason_code=ReasonCode.SOURCE_EMPTY,
            )
            return Context()

        # ---- Build multimodal content parts ----
        content_parts: list[dict] = []
        total_images = 0
        # Global dedup: track every figure path already embedded across all docs
        shown_globally: set[str] = set()

        # Start with the context prompt as the first text part
        content_parts.append({"type": "text", "text": _CONTEXT_PROMPT})

        for rank, doc in enumerate(docs):
            title = doc.get("title") or doc.get("filepath") or doc.get("id") or "Unknown"
            link = doc.get("filepath") or doc.get("url") or ""
            full_text = doc.get("content") or ""
            related_images = doc.get("relatedImages") or []
            image_captions = doc.get("imageCaptions") or ""

            if not full_text:
                AuditEmitter.default().emit_source(
                    selected=False,
                    source_type="azure_ai_search_multimodal",
                    source_reference=link or str(doc.get("id") or ""),
                    source_rank=rank,
                    reason_code=ReasonCode.SOURCE_EMPTY,
                )
                continue
            AuditEmitter.default().emit_source(
                selected=True,
                source_type="azure_ai_search_multimodal",
                source_reference=link or str(doc.get("id") or title),
                source_rank=rank,
                source_excerpt=full_text,
            )

            # Build mapping: figure path → blob URL (from relatedImages list)
            figure_url_map: dict[str, str] = {}
            for blob_url in related_images:
                url_path = urlparse(blob_url).path.lstrip("/")
                figure_url_map[url_path] = blob_url

            # Scan the FULL text for figure tags — BEFORE any truncation so that
            # figures appearing after the display-text boundary are not lost.
            all_figure_matches = list(_FIGURE_RE.finditer(full_text))
            all_figure_paths = [match.group(1).strip() for match in all_figure_matches]
            captions_by_path = _parse_image_captions(image_captions, all_figure_paths)

            remaining_global = self._max_images - total_images
            max_for_doc = min(self._max_images_per_doc, max(remaining_global, 0))

            figures_to_download: list[dict[str, Any]] = []
            for match in all_figure_matches:
                if len(figures_to_download) >= max_for_doc:
                    break
                fig_path = match.group(1).strip()
                if fig_path in shown_globally:
                    continue  # already embedded from a previous doc
                caption = captions_by_path.get(fig_path, "")
                local_text = _extract_local_text(full_text, match.start(), match.end())
                if not _is_relevant_figure(
                    fig_path=fig_path,
                    caption=caption,
                    local_text=local_text,
                    query=query,
                ):
                    logger.info(
                        "[MultimodalSearchContextProvider] Skipping irrelevant figure %s (caption=%r)",
                        fig_path,
                        caption[:120],
                    )
                    continue
                blob_url = figure_url_map.get(fig_path)
                if blob_url:
                    image_hint = _build_image_hint(caption=caption, local_text=local_text)
                    figures_to_download.append({
                        "fig_path": fig_path,
                        "blob_url": blob_url,
                        "start": match.start(),
                        "image_hint": image_hint,
                        "caption": caption,
                        "local_text": local_text,
                    })

            # Download all selected images in parallel
            downloaded: dict[str, str] = {}
            image_hints: dict[str, str] = {}
            if figures_to_download:
                b64_results = await asyncio.gather(
                    *(self._download_image_as_base64(candidate["blob_url"]) for candidate in figures_to_download),
                )
                downloaded_candidates: list[dict[str, Any]] = []
                for candidate, b64 in zip(figures_to_download, b64_results):
                    if b64:
                        downloaded_candidates.append({
                            "query": query,
                            "title": title,
                            "filepath": link,
                            "fig_path": candidate["fig_path"],
                            "caption": candidate["caption"],
                            "local_text": candidate["local_text"],
                            "image_hint": candidate["image_hint"],
                            "image_base64": b64,
                        })

                approved_candidates = await self._filter_downloaded_images(downloaded_candidates)
                for candidate in approved_candidates:
                    fig_path = candidate["fig_path"]
                    downloaded[fig_path] = candidate["image_base64"]
                    image_hints[fig_path] = candidate["image_hint"]
                    # Store for post-response validation
                    self.image_data[fig_path] = candidate["image_base64"]

            # Truncate AFTER scanning — keep track of cutoff position
            if len(full_text) > self._max_content_chars:
                display_text = full_text[:self._max_content_chars] + "..."
                cutoff = self._max_content_chars
            else:
                display_text = full_text
                cutoff = len(full_text)

            # Build content: header + text interleaved with images at original positions
            header = f"### [{title}]({link})" if link else f"### {title}"
            content_parts.append({"type": "text", "text": f"\n\n---\n\n{header}"})

            # Prepend the formatted metadata block before the document content.
            if include_metadata:
                metadata_block = format_custom_metadata(
                    doc.get("custom_metadata") or [],
                    metadata_max_chars,
                    metadata_allowed_keys,
                )
                if metadata_block:
                    content_parts.append({"type": "text", "text": f"\n{metadata_block}"})

            # Inline pass: figures whose <figure> tag falls within the display text
            inline_matches = list(_FIGURE_RE.finditer(display_text))
            shown: set[str] = set()  # local per-doc set (also update shown_globally below)
            last_end = 0
            for match in inline_matches:
                text_before = display_text[last_end:match.start()]
                fig_path = match.group(1).strip()

                if text_before.strip():
                    content_parts.append({"type": "text", "text": text_before})

                if fig_path in downloaded and fig_path not in shown_globally:
                    label = f"📎 Image (embed once only): {fig_path}"
                    hint = image_hints.get(fig_path)
                    if hint:
                        label += f"\nImage hint: {hint}"
                    content_parts.append({"type": "text", "text": label})
                    content_parts.append({"type": "image_url", "url": f"data:image/png;base64,{downloaded[fig_path]}"})
                    shown.add(fig_path)
                    shown_globally.add(fig_path)
                    total_images += 1

                last_end = match.end()

            text_after = display_text[last_end:]
            if text_after.strip():
                content_parts.append({"type": "text", "text": text_after})

            # Beyond-cutoff pass: figures past the truncation boundary are appended
            # so the model can still see and embed them.
            beyond = [
                (candidate["fig_path"], downloaded[candidate["fig_path"]], image_hints.get(candidate["fig_path"], ""))
                for candidate in figures_to_download
                if (
                    candidate["fig_path"] in downloaded
                    and candidate["fig_path"] not in shown
                    and candidate["fig_path"] not in shown_globally
                    and candidate["start"] >= cutoff
                )
            ]
            if beyond:
                content_parts.append({"type": "text", "text": "\n[Additional figures from this section:]"})
                for fig_path, b64, image_hint in beyond:
                    label = f"📎 Image (embed once only): {fig_path}"
                    if image_hint:
                        label += f"\nImage hint: {image_hint}"
                    content_parts.append({"type": "text", "text": label})
                    content_parts.append({"type": "image_url", "url": f"data:image/png;base64,{b64}"})
                    shown_globally.add(fig_path)
                    total_images += 1

        logger.info(
            "[MultimodalSearchContextProvider] Built context: %d text parts, %d images, %.2fs total",
            sum(1 for p in content_parts if p["type"] == "text"),
            total_images,
            time.time() - search_start,
        )

        # If we got images, encode as multimodal; otherwise fall back to plain text
        if total_images > 0:
            encoded = MULTIMODAL_PREFIX + json.dumps(content_parts)
            return Context(messages=[ChatMessage(role=Role.SYSTEM, text=encoded)])
        else:
            # No images — fall back to plain text context (same as SearchContextProvider)
            text_only = "\n".join(p["text"] for p in content_parts if p["type"] == "text")
            return Context(messages=[ChatMessage(role=Role.SYSTEM, text=text_only)])

    async def _filter_downloaded_images(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not candidates or self._classify_images_fn is None:
            return candidates

        unique_candidates: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            unique_candidates.setdefault(candidate["fig_path"], candidate)

        semaphore = asyncio.Semaphore(self._classify_images_concurrency)
        decisions: dict[str, bool] = {}

        async def _classify(candidate: dict[str, Any]) -> None:
            fig_path = candidate["fig_path"]
            async with semaphore:
                try:
                    decisions[fig_path] = await self._classify_images_fn(candidate)
                except Exception as e:
                    logger.warning(
                        "[MultimodalSearchContextProvider] Visual classification failed (%s). Keeping image.",
                        type(e).__name__,
                    )
                    decisions[fig_path] = True

        await asyncio.gather(*(_classify(candidate) for candidate in unique_candidates.values()))
        return [candidate for candidate in candidates if decisions.get(candidate["fig_path"], True)]

    async def _download_image_as_base64(self, blob_url: str) -> Optional[str]:
        """Download an image from Azure Blob Storage and return as base64."""
        try:
            async with AzureBlobClient.from_blob_url(
                blob_url, credential=self._blob_credential
            ) as blob_client:
                stream = await blob_client.download_blob()
                data = await stream.readall()
            return base64.b64encode(data).decode("utf-8")
        except Exception as e:
            logger.warning(
                "[MultimodalSearchContextProvider] Failed to download image (%s)",
                type(e).__name__,
            )
            return None
