import asyncio
import os
import logging
from dotenv import load_dotenv

from connectors import CosmosDBClient
from dependencies import get_config

async def main():
    prompts_directory = os.path.join(os.getcwd(), "prompts")
    if not os.path.isdir(prompts_directory):
        logging.error("[upload_prompts] Prompt directory is unavailable")
        raise SystemExit(1)

    get_config()

    client = CosmosDBClient()
    failed = False

    for root, dirs, files in os.walk(prompts_directory):
        for file in files:
            file_path = os.path.join(root, file)
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except (OSError, UnicodeError) as exc:
                logging.error("[upload_prompts] Prompt read failed (%s)", type(exc).__name__)
                failed = True
                continue

            dir_name = os.path.basename(root)
            data = {
                "id": f"{dir_name}_{os.path.splitext(file)[0]}",
                "content": content,
            }
            if await client.create_document("prompts", data["id"], body=data) is None:
                logging.error("[upload_prompts] Prompt write was not confirmed")
                failed = True

    if failed:
        raise SystemExit(1)

if __name__ == '__main__':
    # Load environment variables from .env file
    load_dotenv()

    asyncio.run(main())
