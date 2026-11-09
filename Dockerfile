# The targets intentionally do not install each other's hosting SDK.
FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm AS runtime-base
WORKDIR /app
RUN python3 -m venv .venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src"
ENV TIKTOKEN_CACHE_DIR="/app/.cache/tiktoken"
COPY requirements*.txt ./
RUN pip install -r requirements.txt -r requirements-tools.txt \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
    && chmod -R a+rX "$TIKTOKEN_CACHE_DIR"
COPY src/ ./src/
COPY contracts/ ./contracts/
COPY VERSION ./

FROM runtime-base AS container_apps
RUN pip install -r requirements-container-apps.txt && pip check
CMD ["python", "-m", "native.durable"]

# Default build remains the Foundry image; select --target container_apps for DTS.
FROM runtime-base AS hosted
RUN pip install -r requirements-hosted.txt && pip check
EXPOSE 8080
CMD ["python", "-m", "native.hosted"]
