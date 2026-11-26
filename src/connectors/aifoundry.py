import logging
import tiktoken

from azure.identity import get_bearer_token_provider
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError
from openai import AsyncAzureOpenAI, RateLimitError
from dependencies import get_config
from connectors.identity_manager import get_identity_manager

class GenAIModelClient:
    """
    Unified GenAI client: 
    - Chat/completions via Azure AI Foundry (AIProjectClient)
    - Embeddings via configurable backend (Azure OpenAI by default)
    Uses Azure AD (Entra ID) for authentication, not API keys.
    """
    def __init__(self):
        cfg = get_config()

        self.foundry_project_endpoint = cfg.get("AI_FOUNDRY_PROJECT_ENDPOINT")
        self.model_endpoint = cfg.get("AI_FOUNDRY_ACCOUNT_ENDPOINT")
        self.chat_deployment = cfg.get("CHAT_DEPLOYMENT_NAME")
        self.embedding_deployment = cfg.get("EMBEDDING_DEPLOYMENT_NAME")
        self.openai_api_version = cfg.get("OPENAI_API_VERSION", "2025-04-01-preview")
        self.max_retries = cfg.get("INFERENCE_MAX_RETRIES", 10, type=int)
        self.max_embedding_tokens = cfg.get("EMBEDDING_MODEL_INPUT_TOKENS", 8192, type=int)
        self.max_chat_tokens = cfg.get("CHAT_INPUT_TOKENS", 128000, type=int)
        self.temperature = cfg.get("CHAT_TEMPERATURE", 0.7, type=float)
        self.top_p = cfg.get("CHAT_TOP_P", 0.95, type=float)
        self.tokenizer_model_name = cfg.get("CHAT_TOKENIZER_MODEL_NAME", "gpt-4o")
        self.embeddings_backend = cfg.get("EMBEDDINGS_BACKEND", "azure_openai")  # "azure_openai" or "foundry"

        # Validate required config
        if not self.foundry_project_endpoint:
            raise ValueError("AI_FOUNDRY_PROJECT_ENDPOINT not set in config")
        if not self.model_endpoint:
            raise ValueError("AI_FOUNDRY_ACCOUNT_ENDPOINT not set in config")
        if not self.chat_deployment:
            raise ValueError("CHAT_DEPLOYMENT_NAME not set in config")
        if not self.embedding_deployment:
            raise ValueError("EMBEDDING_DEPLOYMENT_NAME not set in config")

        # Azure AD (Entra ID) authentication via Singleton
        identity_manager = get_identity_manager()
        credential = identity_manager.get_credential()

        # Foundry client for chat/completions
        self.foundry_client = AIProjectClient(endpoint=self.foundry_project_endpoint, credential=credential)

        # Azure OpenAI Async client for embeddings (default)
        token_provider = get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default"
        )
        self.openai_client = AsyncAzureOpenAI(
            api_version=self.openai_api_version,
            azure_endpoint=self.model_endpoint,
            azure_ad_token_provider=token_provider,
            max_retries=self.max_retries
        )

        # tokenizer for truncation/estimation
        self._tokenizer = tiktoken.encoding_for_model(self.tokenizer_model_name)

    def get_completion(self, prompt: str, max_tokens: int = 1024) -> str:
        """
        Chat/completion using Azure AI Foundry (AIProjectClient).
        """
        short = prompt.replace("\n", " ")[:100]
        logging.info(f"[genai] completion prompt: {short!r}")
        prompt = self._truncate(prompt, self.max_chat_tokens)

        try:
            response = self.foundry_client.chat.completions.create(
                deployment_name=self.chat_deployment,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user",   "content": prompt}
                ],
                temperature=self.temperature,
                top_p=self.top_p,
                max_tokens=max_tokens
                # No API key or model-endpoint headers needed with Entra ID
            )
            return response.choices[0].message.content
        except HttpResponseError as e:
            logging.error(f"[genai] Foundry completion error: {e}")
            raise

    async def get_embeddings(self, text: str, retry_after: bool = True) -> list[float]:
        """
        Embeddings using the configured backend (Azure OpenAI by default, but can be extended).
        """
        short = text.replace("\n", " ")[:100]
        logging.info(f"[genai] embeddings text: {short!r}")

        tok_count = len(self._tokenizer.encode(text))
        if tok_count > self.max_embedding_tokens:
            summary_prompt = (
                f"Reduce to {self.max_embedding_tokens} tokens, preserving coherence: {text}"
            )
            text = self.get_completion(summary_prompt)
            logging.info(f"[genai] text rewritten to fit {self.max_embedding_tokens} tokens")

        if self.embeddings_backend == "azure_openai":
            try:
                resp = await self.openai_client.embeddings.create(
                    input=text,
                    model=self.embedding_deployment
                )
                return resp.data[0].embedding
            except RateLimitError as e:
                import asyncio
                if retry_after and (hdr := e.response.headers.get("retry-after-ms")):
                    ms = int(hdr)
                    logging.warning(f"[genai] embeddings rate-limit, sleeping {ms}ms")
                    await asyncio.sleep(ms / 1000)
                    return await self.get_embeddings(text, retry_after=False)
                logging.error("[genai] embeddings rate limit without header or second failure", exc_info=True)
                raise
        elif self.embeddings_backend == "foundry":
            # Placeholder: Foundry does not support embeddings.create as of now.
            logging.error("[genai] Foundry embeddings backend is not implemented. Please use 'azure_openai' or another supported backend.")
            raise NotImplementedError("Foundry embeddings backend is not implemented in the current SDK.")
        else:
            raise ValueError(f"Unknown embeddings backend: {self.embeddings_backend}")

    def _truncate(self, text: str, max_tokens: int) -> str:
        tokens = self._tokenizer.encode(text)
        if len(tokens) <= max_tokens:
            return text
        logging.info(f"[genai] truncating input from {len(tokens)} to {max_tokens} tokens")
        avg_chars = len(text) / len(tokens)
        cut = int(max_tokens * avg_chars)
        return text[:cut]

_genai_client_instance = None

def get_genai_client() -> GenAIModelClient:
    """
    Returns a unified singleton instance of the GenAIModelClient to reuse HTTP connections.
    """
    global _genai_client_instance
    if _genai_client_instance is None:
        _genai_client_instance = GenAIModelClient()
    return _genai_client_instance
