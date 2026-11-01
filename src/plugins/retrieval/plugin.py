import re
import time
import json
import logging
import asyncio
from typing import Annotated, Optional, List, Dict, Any
from urllib.parse import urlparse

import aiohttp
from azure.core.exceptions import AzureError
from agent_framework import tool
from connectors.identity_manager import get_identity_manager
from dependencies import get_config
from connectors.aifoundry import get_genai_client
from .retrieval_types import (
    VectorIndexRetrievalResult,
    MultimodalVectorIndexRetrievalResult,
    DataPointsResult,
)

class RetrievalPlugin:
    def __init__(self, *, config=None, model=None):
        cfg = config if config is not None else get_config()
        self.aoai = model if model is not None else get_genai_client()
        self.search_top_k = int(cfg.get('SEARCH_RAGINDEX_TOP_K', 3))
        self.search_approach = cfg.get('SEARCH_APPROACH', 'hybrid')
        self.semantic_search_config = cfg.get('SEARCH_SEMANTIC_SEARCH_CONFIG', 'my-semantic-config')
        self.search_service = cfg.get('SEARCH_SERVICE_NAME')
        self.search_index = cfg.get('SEARCH_RAG_INDEX_NAME', 'ragindex')
        self.search_api_version = cfg.get('SEARCH_API_VERSION', '2024-07-01')
        self.use_semantic = cfg.get('SEARCH_USE_SEMANTIC', 'false').lower() == 'true'

    async def _get_azure_search_token(self) -> str:
        try:
            credential = get_identity_manager().get_credential()
            token_obj = await asyncio.to_thread(credential.get_token, "https://search.azure.com/.default")
            return token_obj.token
        except AzureError as e:
            logging.error("Error obtaining Azure Search token (%s).", type(e).__name__)
            raise Exception("Failed to obtain Azure Search token.") from e

    async def _perform_search(self, url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, headers=headers, json=body) as response:
                    response.raise_for_status()
                    return await response.json()
            except (aiohttp.ClientError, ValueError) as e:
                logging.error("Error during asynchronous HTTP request (%s).", type(e).__name__)
                raise Exception("Failed to execute search query.") from e

    @tool(
        name="VectorIndexRetrieve",
        description="Performs a vector search against Azure Cognitive Search and returns the results as a string."
    )
    async def vector_index_retrieve(
        self,
        input: Annotated[str, "Optimized query string based on the user's ask and conversation history, when available"],
        security_ids: Annotated[str, "Security IDs for filtering"] = 'anonymous'
    ) -> VectorIndexRetrievalResult:
        search_results: List[str] = []
        error_message: Optional[str] = None
        search_query = input

        try:
            start_time = time.time()
            logging.info("[vector_index_retrieve] Generating question embeddings.")
            embeddings_query = await self.aoai.get_embeddings(search_query)
            logging.info(f"[vector_index_retrieve] Finished generating embeddings in {round(time.time() - start_time, 2)} seconds")

            azure_search_token = await self._get_azure_search_token()

            body: Dict[str, Any] = {
                "select": "title, content, url, filepath, chunk_id",
                "top": self.search_top_k
            }
            if self.search_approach == "term":
                body["search"] = search_query
            elif self.search_approach == "vector":
                body["vectorQueries"] = [{
                    "kind": "vector",
                    "vector": embeddings_query,
                    "fields": "contentVector",
                    "k": self.search_top_k
                }]
            elif self.search_approach == "hybrid":
                body["search"] = search_query
                body["vectorQueries"] = [{
                    "kind": "vector",
                    "vector": embeddings_query,
                    "fields": "contentVector",
                    "k": self.search_top_k
                }]

            filter_str = (
                f"metadata_security_id/any(g:search.in(g, '{security_ids}')) "
                f"or not metadata_security_id/any()"
            )
            body["filter"] = filter_str

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {azure_search_token}'
            }

            search_url = (
                f"https://{self.search_service}.search.windows.net/indexes/{self.search_index}/docs/search"
                f"?api-version={self.search_api_version}"
            )

            response_json = await self._perform_search(search_url, headers, body)

            if response_json.get('value'):
                for doc in response_json['value']:
                    url = doc.get('url', '')
                    uri = re.sub(r'https://[^/]+\.blob\.core\.windows\.net', '', url)
                    content_str = doc.get('content', '').strip()
                    search_results.append(f"{uri}: {content_str}\n")
        except Exception as e:
            error_message = f"Exception occurred: {type(e).__name__}"
            logging.error("[vector_index_retrieve] %s", error_message)

        sources = ' '.join(search_results)
        return VectorIndexRetrievalResult(result=sources, error=error_message)

    def extract_captions(self, str_captions):
        pattern = r"\[.*?\]:\s(.*?)(?=\[.*?\]:|$)"
        matches = re.findall(pattern, str_captions, re.DOTALL)
        return [match.strip() for match in matches]

    def replace_image_filenames_with_urls(self, content: str, related_images: list) -> str:
        for image_url in related_images:
            parsed_url = urlparse(image_url)
            image_path = parsed_url.path.lstrip('/')
            content = content.replace(image_path, image_url)
        return content

    @tool(
        name="MultimodalVectorIndexRetrieve",
        description="Performs a multimodal vector search and returns texts, images, and captions."
    )
    async def multimodal_vector_index_retrieve(
        self,
        input: Annotated[str, "Optimized query string based on the user's ask and conversation history, when available"],
        security_ids: Annotated[str, "Security IDs for filtering"] = 'anonymous'
    ) -> MultimodalVectorIndexRetrievalResult:
        text_results: List[str] = []
        image_urls: List[List[str]] = []
        captions: List[List[str]] = []
        error_message: Optional[str] = None

        try:
            start_time = time.time()
            embeddings_query = await self.aoai.get_embeddings(input)
            logging.info(f"[multimodal_vector_index_retrieve] Query embeddings took {round(time.time() - start_time, 2)} seconds")
        except Exception as e:
            error_message = f"Error generating embeddings: {type(e).__name__}"
            logging.error("[multimodal_vector_index_retrieve] %s", error_message)
            return MultimodalVectorIndexRetrievalResult(
                texts=[],
                images=[],
                captions=[],
                error=error_message
            )

        try:
            azure_search_token = await self._get_azure_search_token()
        except Exception as e:
            error_message = f"Error acquiring token for Azure Search: {type(e).__name__}"
            logging.error("[multimodal_vector_index_retrieve] %s", error_message)
            return MultimodalVectorIndexRetrievalResult(
                texts=[],
                images=[],
                captions=[],
                error=error_message
            )

        body: Dict[str, Any] = {
            "select": "title, content, filepath, url, imageCaptions, relatedImages",
            "top": self.search_top_k,
            "vectorQueries": [
                {
                    "kind": "vector",
                    "vector": embeddings_query,
                    "fields": "contentVector",
                    "k": self.search_top_k
                },
                {
                    "kind": "vector",
                    "vector": embeddings_query,
                    "fields": "captionVector",
                    "k": self.search_top_k
                }
            ]
        }

        if self.use_semantic and self.search_approach != "vector":
            body["queryType"] = "semantic"
            body["semanticConfiguration"] = self.semantic_search_config

        filter_str = (
            f"metadata_security_id/any(g:search.in(g, '{security_ids}')) "
            "or not metadata_security_id/any()"
        )
        body["filter"] = filter_str

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {azure_search_token}'
        }

        search_url = (
            f"https://{self.search_service}.search.windows.net"
            f"/indexes/{self.search_index}/docs/search"
            f"?api-version={self.search_api_version}"
        )

        try:
            response_json = await self._perform_search(search_url, headers, body)
            for doc in response_json.get('value', []):
                content = doc.get('content', '')
                str_captions = doc.get('imageCaptions', '')
                document_captions = self.extract_captions(str_captions)
                url = doc.get('url', '')
                uri = re.sub(r'https://[^/]+\.blob\.core\.windows\.net', '', url)
                text_result = f"{uri}: {content.strip()}"
                content = self.replace_image_filenames_with_urls(content, doc.get('relatedImages', []))
                text_results.append(text_result)
                captions.append(document_captions)
                image_urls.append(doc.get('relatedImages', []))
        except Exception as e:
            error_message = f"Exception in retrieval: {type(e).__name__}"
            logging.error("[multimodal_vector_index_retrieve] %s", error_message)

        return MultimodalVectorIndexRetrievalResult(
            texts=text_results,
            images=image_urls,
            captions=captions,
            error=error_message
        )

    @tool(
        name="GetDataPointsFromChatLog",
        description="Extracts data points (e.g., filenames) from a chat log."
    )
    def get_data_points_from_chat_log(
        self,
        chat_log: Annotated[list, "Chat log as a list of message dicts"]
    ) -> DataPointsResult:
        request_call_id_pattern = re.compile(r"id='([^']+)'")
        request_function_name_pattern = re.compile(r"name='([^']+)'")
        exec_call_id_pattern = re.compile(r"call_id='([^']+)'")
        exec_content_pattern = re.compile(r"content='(.+?)', call_id=", re.DOTALL)

        allowed_extensions = ['vtt', 'xlsx', 'xls', 'pdf', 'docx', 'pptx', 'png', 'jpeg', 'jpg', 'bmp', 'tiff']
        filename_pattern = re.compile(
            rf"([^\s:]+\.(?:{'|'.join(allowed_extensions)})\s*:\s*.*?)(?=[^\s:]+\.(?:{'|'.join(allowed_extensions)})\s*:|$)",
            re.IGNORECASE | re.DOTALL
        )

        relevant_call_ids = set()
        data_points = []

        for msg in chat_log:
            if msg["message_type"] == "ToolCallRequestEvent":
                content = msg["content"][0]
                call_id_match = request_call_id_pattern.search(content)
                function_name_match = request_function_name_pattern.search(content)
                if call_id_match and function_name_match:
                    if function_name_match.group(1) == "vector_index_retrieve_wrapper":
                        relevant_call_ids.add(call_id_match.group(1))
            elif msg["message_type"] == "ToolCallExecutionEvent":
                content = msg["content"][0]
                call_id_match = exec_call_id_pattern.search(content)
                if call_id_match and call_id_match.group(1) in relevant_call_ids:
                    content_part_match = exec_content_pattern.search(content)
                    if not content_part_match:
                        continue
                    content_part = content_part_match.group(1)
                    try:
                        parsed = json.loads(content_part)
                        texts = parsed.get("texts", [])
                    except json.JSONDecodeError:
                        texts = [re.split(r'["\']images["\']\s*:\s*\[', content_part, 1, re.IGNORECASE)[0]]
                    for text in texts:
                        text = bytes(text, "utf-8").decode("unicode_escape")
                        for match in filename_pattern.findall(text):
                            extracted = match.strip(" ,\\\"").lstrip("[").rstrip("],")
                            if extracted:
                                data_points.append(extracted)
        return DataPointsResult(data_points=data_points)