import logging
from datetime import datetime, timezone
from typing import List, Optional

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.core.exceptions import AzureError
from dependencies import get_config

class CosmosDBClient:
    """
    CosmosDBClient with persistent connection pooling.
    A single CosmosClient is created once and reused across all operations,
    eliminating per-request TCP/TLS handshake overhead.
    """

    def __init__(self):
        """
        Initializes the Cosmos DB client with a persistent connection.
        """
        # ==== Load all config parameters in one place ====
        self.cfg = get_config()
        self.database_account_name = self.cfg.get("DATABASE_ACCOUNT_NAME")
        self.database_name = self.cfg.get("DATABASE_NAME")
        self.db_uri = f"https://{self.database_account_name}.documents.azure.com:443/"
        # ==== End config block ====

        # Persistent CosmosClient — reuses TCP connections across all operations
        self._client = CosmosClient(self.db_uri, credential=self.cfg.aiocredential)

    def _get_container(self, container_name):
        """Returns a container client from the persistent connection."""
        db = self._client.get_database_client(database=self.database_name)
        return db.get_container_client(container_name)

    async def close(self):
        """Close capability-owned clients without touching the legacy singleton."""
        await self._client.close()

    async def list_documents(self, container_name) -> list:
        """
        Lists all documents from the given container.
        """
        container = self._get_container(container_name)

        # Correct usage without the outdated argument
        query = "SELECT * FROM c"
        items_iterable = container.query_items(query=query, partition_key=None)

        documents = []
        async for item in items_iterable:
            documents.append(item)

        return documents

    async def get_document(self, container, key, partition_key=None) -> dict:
        """Retrieve a document by key with optional partition key.
        
        If partition_key is None, uses key as partition key for backward compatibility.
        """
        pk_value = partition_key if partition_key is not None else key
        container_client = self._get_container(container)
        try:
            document = await container_client.read_item(item=key, partition_key=pk_value)
            logging.info(f"[cosmosdb] document {key} retrieved.")
        except AzureError as e:
            document = None
            logging.debug("[cosmosdb] document %s unavailable (%s)", key, type(e).__name__)
        return document

    async def document_id_exists(self, container, key) -> bool:
        """Return True if any partition holds a document with this id.

        Raises AzureError on failure so callers can fail closed.
        """
        container_client = self._get_container(container)
        items = container_client.query_items(
            query="SELECT VALUE c.id FROM c WHERE c.id = @id",
            parameters=[{"name": "@id", "value": key}],
        )
        async for _ in items:
            return True
        return False

    async def create_document(self, container, key, body=None, partition_key=None) -> dict:
        """Create a new document with optional partition key.
        
        If partition_key is provided, it's stored as principal_id in the document body.
        """
        container_client = self._get_container(container)
        try:
            if body is None:
                body = {"id": key}
            else:
                body["id"] = key  # ensure the document id is set
            body["lastUpdated"] = datetime.now(timezone.utc).isoformat()
            if partition_key is not None:
                body["principal_id"] = partition_key
            document = await container_client.create_item(body=body)
            logging.info(f"[cosmosdb] document {key} created.")
        except AzureError as e:
            document = None
            logging.error("[cosmosdb] error creating document %s (%s)", key, type(e).__name__)
        return document

    async def update_document(self, container, document) -> dict:
        container_client = self._get_container(container)
        doc_id = document.get("id") if isinstance(document, dict) else None
        try:
            document["lastUpdated"] = datetime.now(timezone.utc).isoformat()
            document = await container_client.replace_item(item=document["id"], body=document)
            logging.info(f"[cosmosdb] document updated.")
        except CosmosHttpResponseError as e:
            document = None
            status = getattr(e, "status_code", "unknown")
            if status == 404:
                # Replace can legitimately fail when the conversation doc was not created yet.
                logging.warning(
                    "[cosmosdb] update skipped: document not found (container=%s, id=%s, status=%s)",
                    container,
                    doc_id,
                    status,
                )
            else:
                logging.warning(
                    "[cosmosdb] update failed (container=%s, id=%s, status=%s): %s",
                    container,
                    doc_id,
                    status,
                    e.__class__.__name__,
                )
        except AzureError as e:
            document = None
            logging.warning(
                "[cosmosdb] could not update document (container=%s, id=%s): %s",
                container,
                doc_id,
                e.__class__.__name__,
            )
        return document


_cosmosdb_client_instance = None

def get_cosmosdb_client() -> CosmosDBClient:
    """Returns a singleton CosmosDBClient to reuse TCP connections."""
    global _cosmosdb_client_instance
    if _cosmosdb_client_instance is None:
        _cosmosdb_client_instance = CosmosDBClient()
    return _cosmosdb_client_instance


async def query_user_conversations(
    principal_id: str,
    skip: int,
    limit: int,
    name: Optional[str] = None,
) -> List[dict]:
    """Return the requested span of a user's conversations, excluding soft-deleted ones."""
    cosmos = get_cosmosdb_client()
    container_name = cosmos.cfg.get("CONVERSATIONS_DATABASE_CONTAINER", "conversations")
    container = cosmos._get_container(container_name)

    if name:
        query = (
            "SELECT c.id, c.name, c._ts, c.lastUpdated FROM c"
            " WHERE c.principal_id = @principal_id"
            " AND CONTAINS(c.name, @name)"
            " AND (NOT IS_DEFINED(c.isDeleted) OR c.isDeleted = false)"
            " ORDER BY c._ts DESC OFFSET @skip LIMIT @limit"
        )
        parameters = [
            {"name": "@principal_id", "value": principal_id},
            {"name": "@name", "value": name},
            {"name": "@skip", "value": skip},
            {"name": "@limit", "value": limit},
        ]
    else:
        query = (
            "SELECT c.id, c.name, c._ts, c.lastUpdated FROM c"
            " WHERE c.principal_id = @principal_id"
            " AND (NOT IS_DEFINED(c.isDeleted) OR c.isDeleted = false)"
            " ORDER BY c._ts DESC OFFSET @skip LIMIT @limit"
        )
        parameters = [
            {"name": "@principal_id", "value": principal_id},
            {"name": "@skip", "value": skip},
            {"name": "@limit", "value": limit},
        ]

    items_iterable = container.query_items(
        query=query,
        parameters=parameters,
        partition_key=principal_id,
    )

    conversations = []
    async for document in items_iterable:
        conversations.append(document)

    logging.debug("[CosmosDB] User %s retrieved %d conversations", principal_id, len(conversations))
    return conversations


async def read_user_conversation(conversation_id: str, principal_id: str) -> Optional[dict]:
    """Return the conversation document if the partition matches and not soft-deleted."""
    cosmos = get_cosmosdb_client()
    container_name = cosmos.cfg.get("CONVERSATIONS_DATABASE_CONTAINER", "conversations")
    container = cosmos._get_container(container_name)

    try:
        doc = await container.read_item(item=conversation_id, partition_key=principal_id)
        if doc.get("isDeleted") is True:
            logging.debug("[CosmosDB] Conversation %s is marked as deleted", conversation_id)
            return None
        return doc
    except AzureError as exc:
        logging.debug(
            "[CosmosDB] Conversation %s unavailable for %s (%s)",
            conversation_id, principal_id, type(exc).__name__,
        )
        return None


async def update_conversation_name(conversation_id: str, principal_id: str, new_name: str) -> Optional[dict]:
    """Update the name of a conversation (soft-deleted conversations cannot be updated)."""
    cosmos = get_cosmosdb_client()
    container_name = cosmos.cfg.get("CONVERSATIONS_DATABASE_CONTAINER", "conversations")
    container = cosmos._get_container(container_name)

    try:
        doc = await container.read_item(item=conversation_id, partition_key=principal_id)
        if doc.get("isDeleted") is True:
            logging.warning("[CosmosDB] Cannot update soft-deleted conversation %s", conversation_id)
            return None
        doc["name"] = new_name
        doc["lastUpdated"] = datetime.now(timezone.utc).isoformat()
        updated_doc = await container.replace_item(item=conversation_id, body=doc)
        logging.info("[CosmosDB] Conversation %s name updated", conversation_id)
        return updated_doc
    except AzureError as exc:
        logging.error(
            "[CosmosDB] Error updating conversation %s (%s)",
            conversation_id, type(exc).__name__,
        )
        return None


async def soft_delete_conversation(conversation_id: str, principal_id: str) -> Optional[dict]:
    """Soft delete a conversation by setting isDeleted=True."""
    cosmos = get_cosmosdb_client()
    container_name = cosmos.cfg.get("CONVERSATIONS_DATABASE_CONTAINER", "conversations")
    container = cosmos._get_container(container_name)

    try:
        doc = await container.read_item(item=conversation_id, partition_key=principal_id)
        doc["isDeleted"] = True
        doc["deletedAt"] = datetime.now(timezone.utc).isoformat()
        doc["lastUpdated"] = doc["deletedAt"]
        updated_doc = await container.replace_item(item=conversation_id, body=doc)
        logging.info("[CosmosDB] Conversation %s soft deleted", conversation_id)
        return updated_doc
    except AzureError as exc:
        logging.error(
            "[CosmosDB] Error soft deleting conversation %s (%s)",
            conversation_id, type(exc).__name__,
        )
        return None
