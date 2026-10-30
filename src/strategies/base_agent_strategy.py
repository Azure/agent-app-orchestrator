import logging
import os
import re
import json

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Mapping, Optional
from abc import ABC, abstractmethod
from azure.ai.projects.aio import AIProjectClient
from connectors.appconfig import AppConfigClient
from connectors.cosmosdb import get_cosmosdb_client
from connectors.identity_manager import get_identity_manager
from dependencies import get_config
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError
from pathlib import Path
from .agent_strategies import AgentStrategies


_HOSTED_RUNTIME_CONSTRUCTION: ContextVar[bool] = ContextVar(
    "hosted_runtime_construction",
    default=False,
)


@contextmanager
def hosted_runtime_construction():
    """Construct a strategy without classic Cosmos-backed runtime state."""
    token = _HOSTED_RUNTIME_CONSTRUCTION.set(True)
    try:
        yield
    finally:
        _HOSTED_RUNTIME_CONSTRUCTION.reset(token)

class BaseAgentStrategy(ABC):
    """
    Base strategy for agents.
    """
    strategy_type : AgentStrategies
    conversation : Dict
    user_context : Dict
    foundry_call_id : Optional[str]

    def __init__(self):
        """
        Initializes endpoint, model name, credentials, and default event handler.
        """
        # App configuration
        self.cfg : AppConfigClient = get_config()
        self.project_endpoint = self.cfg.get("AI_FOUNDRY_PROJECT_ENDPOINT")
        self.account_endpoint = self.cfg.get("AI_FOUNDRY_ACCOUNT_ENDPOINT")
        self.model_name = self.cfg.get("CHAT_DEPLOYMENT_NAME")
        self.prompt_source = self.cfg.get("PROMPT_SOURCE", "file")
        self.openai_api_version = self.cfg.get("OPENAI_API_VERSION", "2025-04-01-preview")   
        self.hosted_runtime = _HOSTED_RUNTIME_CONSTRUCTION.get()
        self.profile_memory_enabled = not self.hosted_runtime

        if self.hosted_runtime and self.prompt_source == "cosmos":
            raise EnvironmentError(
                "Hosted runtime requires file-backed prompts; "
                "PROMPT_SOURCE='cosmos' would reintroduce a Cosmos dependency."
            )

        logging.debug(f"[base_agent_strategy] Project endpoint: {self.project_endpoint}")
        logging.debug(f"[base_agent_strategy] Model name: {self.model_name}")

        if not self.project_endpoint or not self.model_name:
            raise EnvironmentError(
                "Both AI_FOUNDRY_PROJECT_ENDPOINT and CHAT_DEPLOYMENT_NAME must be set"
            )

        # Azure AD/Entra ID credentials from Singleton
        identity_manager = get_identity_manager()
        self.credential = identity_manager.get_aio_credential()

        # Initialize the async AIProjectClient
        self.project_client = AIProjectClient(
            endpoint=self.project_endpoint,
            credential=self.credential
        )

        self.cosmos = None if self.hosted_runtime else get_cosmosdb_client()

        self.user_context = {}
        # Opaque Foundry hosted-agent call id (x-agent-foundry-call-id),
        # set only for Toolbox-integrated hosted strategies after HTTP-layer
        # validation. See util.foundry_platform for the security rationale.
        self.foundry_call_id: Optional[str] = None

    @classmethod
    async def create(cls):
        return cls()

    def _eligible_profile_user_id(self) -> Optional[str]:
        """Automatic access is suspended: legacy keys have no trusted binding."""
        logging.info("profile_access_disabled_unverified_binding")
        self._user_memory = None
        return None

    def _get_profile_user_id(self) -> Optional[str]:
        """Reject absent/synthetic profile keys without selecting another identity.

        This is a negative eligibility guard, not authentication of legacy
        conversation user_id values. Preserve an existing key verbatim.
        """
        if not self.profile_memory_enabled:
            return None
        user_id = self.conversation.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip() or user_id.strip() == "default_user":
            return None
        return user_id
    
    @abstractmethod
    async def initiate_agent_flow(self, user_message: str):
        """
        Kick off the agent execution pipeline and stream responses.

        This method will:
        1. Manage and update the conversation context.
        2. Dispatch the user's input through the agent pipeline.
        3. Yield response segments as they are generated.

        Args:
            user_message (str): The input message from the end user.

        Yields:
            AsyncIterator[str]: An async iterator over chunks of the agent’s response.
        """

    async def _read_prompt(
        self,
        prompt_name: str,
        placeholders: Optional[Mapping[str, str]] = None,
        *,
        use_jinja2: Optional[bool] = None,
        jinja2_context: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Load and process a prompt from the configured source."""

        placeholders_dict: Dict[str, str] = dict(placeholders or {})

        if self.prompt_source == "file":
            prompt_dir = Path(self._prompt_dir())
            jinja2_path = prompt_dir / f"{prompt_name}.jinja2"
            txt_path = prompt_dir / f"{prompt_name}.txt"

            if use_jinja2 is True:
                if not jinja2_path.exists():
                    raise FileNotFoundError(
                        f"Jinja2 template '{prompt_name}.jinja2' not found in {prompt_dir}"
                    )
                context = dict(jinja2_context or placeholders_dict)
                return await self._read_prompt_jinja2(prompt_name, context)

            if use_jinja2 is False:
                if not txt_path.exists():
                    raise FileNotFoundError(
                        f"Prompt file '{prompt_name}.txt' not found in {prompt_dir}"
                    )
                return await self._read_prompt_file(prompt_name, placeholders_dict)

            # Auto-detect format when use_jinja2 is None
            if jinja2_path.exists():
                context = dict(jinja2_context or placeholders_dict)
                return await self._read_prompt_jinja2(prompt_name, context)

            if txt_path.exists():
                return await self._read_prompt_file(prompt_name, placeholders_dict)

            raise FileNotFoundError(
                f"Prompt '{prompt_name}' not found in {prompt_dir}. "
                f"Tried: {prompt_name}.jinja2, {prompt_name}.txt"
            )

        if self.prompt_source == "cosmos":
            return await self._read_prompt_cosmos(prompt_name, placeholders_dict)

        raise ValueError(f"Unsupported prompt_source: {self.prompt_source!r}")

    async def _read_prompt_file(self, prompt_name, placeholders=None):
        """
        Load and process a prompt file, applying strategy-based variants and placeholder replacements.

        This method reads a prompt file associated with a given agent, supporting optional variants
        (e.g., audio-optimized or text-only) and dynamic placeholder substitution.

        **Prompt Directory Structure**:
        - Prompts are stored in the `prompts/` directory.
        - If a strategy type is defined (`self.strategy_type`), the file is expected in a subdirectory:
          `prompts/<strategy_type>/`.

        **Prompt File Naming Convention**:
        - The filename is based on the provided `prompt_name`: `<prompt_name>.txt`.
        - You can pre-define variants externally using names like `<prompt_name>_audio.txt` or 
          `<prompt_name>_text_only.txt`, but this method does not automatically append suffixes. 
          Suffix logic must be handled when building `prompt_name`.

        **Placeholder Substitution**:
        - If a `placeholders` dictionary is provided, placeholders in the format `{{key}}` are replaced by
          their corresponding values.
        - If any `{{key}}` remains after substitution, the method checks for a fallback file:
          `prompts/common/<key>.txt`. If found, its content replaces the placeholder.
        - If no replacement is available, a warning is logged.

        **Example**:
        For `prompt_name='agent1_audio'` and `self.strategy_type='customer_service'`, the file path would be:
        `prompts/customer_service/agent1_audio.txt`

        **Parameters**:
        - prompt_name (str): The base name of the prompt file (without path, but may include variant suffix).
        - placeholders (dict, optional): Mapping of placeholder names to their substitution values.

        **Returns**:
        - str: Final content of the prompt with placeholders replaced.

        **Raises**:
        - FileNotFoundError: If the specified prompt file does not exist.
        """
 
        # Construct the prompt file path
        prompt_file_path = os.path.join(self._prompt_dir(), f"{prompt_name}.txt")

        if not os.path.exists(prompt_file_path):
            logging.error(f"[base_agent_strategy] Prompt file '{prompt_name}' not found: {prompt_file_path}.")
            raise FileNotFoundError(f"Prompt file '{prompt_name}' not found.")

        logging.info(f"[base_agent_strategy] Using prompt file path: {prompt_file_path}")
        
        # Read and process the selected prompt file
        with open(prompt_file_path, "r") as f:
            prompt = f.read().strip()
            
            # Replace placeholders provided in the 'placeholders' dictionary
            if placeholders:
                for key, value in placeholders.items():
                    prompt = prompt.replace(f"{{{{{key}}}}}", value)
            
            # Find any remaining placeholders in the prompt
            pattern = r"\{\{([^}]+)\}\}"
            matches = re.findall(pattern, prompt)
            
            # Process each unmatched placeholder
            for placeholder_name in set(matches):
                # Skip if placeholder was already replaced
                if placeholders and placeholder_name in placeholders:
                    continue
                # Look for a corresponding file in 'prompts/common'
                common_file_path = os.path.join("prompts", "common", f"{placeholder_name}.txt")
                if os.path.exists(common_file_path):
                    with open(common_file_path, "r") as pf:
                        placeholder_content = pf.read().strip()
                        prompt = prompt.replace(f"{{{{{placeholder_name}}}}}", placeholder_content)
                else:
                    # Log a warning if the placeholder cannot be replaced
                    logging.warning(
                        f"[base_agent_strategy] Placeholder '{{{{{placeholder_name}}}}}' could not be replaced."
                    )
            return prompt

    async def _read_prompt_cosmos(self, prompt_name, placeholders=None):

        namespace = self._prompt_namespace()
        logging.info(f"[base_agent_strategy] Using cosmo prompt : {namespace} : {prompt_name}")

        prompt_json = await self.cosmos.get_document("prompts", f"{namespace}_{prompt_name}")
        prompt = prompt_json.get("content", "")
            
        # Replace placeholders provided in the 'placeholders' dictionary
        if placeholders:
            for key, value in placeholders.items():
                prompt = prompt.replace(f"{{{{{key}}}}}", value)
        
        # Find any remaining placeholders in the prompt
        pattern = r"\{\{([^}]+)\}\}"
        matches = re.findall(pattern, prompt)
        
        # Process each unmatched placeholder
        for placeholder_name in set(matches):
            # Skip if placeholder was already replaced
            if placeholders and placeholder_name in placeholders:
                continue
            
            placeholder_content = await self.cosmos.get_document("prompts", f"placeholder_{placeholder_name}")
            prompt = prompt.replace(f"{{{{{placeholder_name}}}}}", placeholder_content)
            
        return prompt

    def _prompt_namespace(self) -> str:
        """Return the prompt namespace to use for this strategy.

        Subclasses may override this to share prompt directories across strategies.
        """
        return self.strategy_type.value
    
    def _prompt_dir(self):
        """
        Get the directory path for prompts based on strategy type.
        
        Returns:
            str: Absolute path to the strategy's prompt directory
            
        Raises:
            ValueError: If strategy_type is not defined
            FileNotFoundError: If the prompt directory does not exist
        """
        # __file__ is .../src/strategies/base_agent_strategy.py
        # go up to the 'src' folder, then into 'prompts/<strategy>'
        src_folder = Path(__file__).resolve().parent.parent  # .../src
        if not hasattr(self, 'strategy_type'):
            raise ValueError("strategy_type is not defined")     
        prompt_folder = src_folder / "prompts" / self._prompt_namespace()
        if not prompt_folder.exists():
            raise FileNotFoundError(f"Prompt directory not found: {prompt_folder}")
        return str(prompt_folder)

    async def _read_prompt_jinja2(self, prompt_name, context=None):
        """
        Load and render a Jinja2 template for prompts.
        
        This method supports Jinja2 templates with conditional logic, loops, and variables.
        Templates should have .jinja2 extension and be located in the strategy's prompt directory.
        
        **Prompt Directory Structure**:
        - Templates are stored in `prompts/<strategy_type>/` directory
        - Template files use `.jinja2` extension (e.g., `main.jinja2`)
        
        **Template Context**:
        - Pass a dictionary with variables that will be available in the template
        - Common variables: user_context, config values, etc.
        
        **Example Template Usage**:
        ```jinja2
        {% if user_context %}
        Personalize the answer using the user context...
        {% else %}
        Answer using only the retrieved documents...
        {% endif %}
        ```
        
        **Parameters**:
        - prompt_name (str): The base name of the template file (without .jinja2 extension)
        - context (dict, optional): Dictionary of variables to pass to the template
        
        **Returns**:
        - str: Rendered prompt content
        
        **Raises**:
        - FileNotFoundError: If the template file does not exist
        - TemplateError: If there's a syntax error in the template
        """
        
        # Setup Jinja2 environment
        prompt_dir = self._prompt_dir()
        
        try:
            env = Environment(
                loader=FileSystemLoader(prompt_dir),
                autoescape=True,
                undefined=StrictUndefined,  # Raises error if undefined variable is used
                trim_blocks=True,           # Remove first newline after template tag
                lstrip_blocks=True          # Strip leading spaces/tabs from block tags
            )
            
            # Try to load the template
            template_name = f"{prompt_name}.jinja2"
            template = env.get_template(template_name)
            
            logging.info(f"[base_agent_strategy] Rendering Jinja2 template: {os.path.join(prompt_dir, template_name)}")
            
            # Render with provided context
            rendered = template.render(context or {})
            
            return rendered.strip()
            
        except FileNotFoundError:
            logging.error(f"[base_agent_strategy] Jinja2 template '{prompt_name}' not found in {prompt_dir}")
            raise FileNotFoundError(f"Jinja2 template '{prompt_name}.jinja2' not found in {prompt_dir}")
        except TemplateError as e:
            logging.error(f"[base_agent_strategy] Error rendering Jinja2 template '{prompt_name}': {e}")
            raise TemplateError(f"Error rendering template '{prompt_name}': {e}")

    def _get_model(self, model_name: str = 'CHAT_DEPLOYMENT_NAME') -> Dict:
        model_deployments = self.cfg.get("MODEL_DEPLOYMENTS", default='[]').replace("'", "\"")

        try:
            print(f"Model deployments: {model_deployments}")
            logging.info(f"Model deployments: {model_deployments}")

            json_model_deployments = json.loads(model_deployments)

            #get the canonical_name of 'CHAT_DEPLOYMENT_NAME'
            for deployment in json_model_deployments:
                if deployment.get("canonical_name") == model_name:
                    return deployment
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding JSON for model deployments: {e}")
            raise ValueError(f"Invalid model deployments configuration: {model_deployments}")
            
        return None
