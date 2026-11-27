import logging
import re
from azure.identity.aio import ManagedIdentityCredential, AzureCliCredential, ChainedTokenCredential
from azure.keyvault.secrets.aio import SecretClient as AsyncSecretClient
from azure.core.exceptions import AzureError, ResourceNotFoundError, ClientAuthenticationError
from dependencies import get_config

##########################################################
# KEY VAULT 
##########################################################

async def get_secret(name):
    try:

        cfg = get_config()
        KVUri = cfg.get("KEY_VAULT_URI")
        async with ChainedTokenCredential(
                ManagedIdentityCredential(),
                AzureCliCredential()
            ) as credential:
            async with AsyncSecretClient(vault_url=KVUri, credential=credential) as client:
                retrieved_secret = await client.get_secret(name)
                value = retrieved_secret.value
        return value    
    except (KeyError, ValueError) as e:
        logging.info("Key Vault configuration or request input unavailable (%s).", type(e).__name__)
        return None
    except ClientAuthenticationError:
        logging.info("Authentication failed. Please check your credentials.")
        return None
    except ResourceNotFoundError:
        logging.info("Requested secret not found in the Key Vault.")
        return None
    except AzureError as e:
        logging.info("Key Vault SDK request failed (%s).", type(e).__name__)
        return None

def generate_valid_secret_name(base_name: str) -> str:
    """
    Generate a valid secret name that contains only alphanumeric characters and dashes.
    
    Args:
        base_name (str): The base name to convert into a valid secret name.
    
    Returns:
        str: A sanitized secret name with only valid characters.
    """
    # Replace any non-alphanumeric characters with dashes
    sanitized_name = re.sub(r'[^a-zA-Z0-9-]', '-', base_name)
    # Ensure it does not start or end with a dash and limit its length
    sanitized_name = sanitized_name.strip('-')[:63]  # Max length for Azure Key Vault secret names is 63
    return sanitized_name or "default-secret"