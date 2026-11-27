import logging
from azure.identity import ChainedTokenCredential, ManagedIdentityCredential, AzureCliCredential
from azure.identity.aio import (
    ChainedTokenCredential as AsyncChainedTokenCredential,
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
    AzureCliCredential as AsyncAzureCliCredential,
)
import os

class IdentityManager:
    """
    Singleton for managing Azure Credentials.
    Prevents redundant calls to the identity endpoints by reusing the exact same 
    credential token instances across all connections in the lifecycle.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(IdentityManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
            
        logging.getLogger("azure.identity").setLevel(logging.WARNING)
        logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
        
        # Load environment identity if configured (used in AKS or App Services)
        self.client_id = os.environ.get("AZURE_CLIENT_ID", None)

        logging.debug("[IdentityManager] Initializing Global Identity Singleton...")

        # Sync credentials
        mi_cred = ManagedIdentityCredential(client_id=self.client_id) if self.client_id else ManagedIdentityCredential()
        self._credential = ChainedTokenCredential(
            mi_cred,
            AzureCliCredential()
        )

        # Async credentials
        async_mi_cred = AsyncManagedIdentityCredential(client_id=self.client_id) if self.client_id else AsyncManagedIdentityCredential()
        self._aio_credential = AsyncChainedTokenCredential(
            async_mi_cred,
            AsyncAzureCliCredential()
        )

        self._initialized = True
        logging.debug("[IdentityManager] Global Identity Singleton Initialized successfully.")

    def get_credential(self) -> ChainedTokenCredential:
        """Returns the synchronous chained credential."""
        return self._credential

    def get_aio_credential(self) -> AsyncChainedTokenCredential:
        """Returns the asynchronous chained credential."""
        return self._aio_credential

# Initialize single instance immediately
identity_manager = IdentityManager()

def get_identity_manager() -> IdentityManager:
    return identity_manager
