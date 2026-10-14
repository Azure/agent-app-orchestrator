"""Opt-in verified TLS compatibility for configuration bootstrap endpoints."""

import logging
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

import requests
from azure.core.pipeline.transport import RequestsTransport
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection

logger = logging.getLogger(__name__)


def _tls_context(cafile: str | None = None) -> ssl.SSLContext:
    context = (
        ssl.create_default_context(cafile=cafile)
        if cafile is not None
        else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ecdh_curve("prime256v1")
    return context


class _TLSCompatibilityAdapter(HTTPAdapter):
    def __init__(self) -> None:
        self.tls_context = _tls_context(requests.certs.where())
        super().__init__()

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **kwargs: Any
    ) -> None:
        kwargs["socket_options"] = [
            *HTTPConnection.default_socket_options,
            (socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1200),
        ]
        super().init_poolmanager(connections, maxsize, block, **kwargs)

    def build_connection_pool_key_attributes(
        self,
        request: requests.PreparedRequest,
        verify: bool | str,
        cert: str | tuple[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if verify is False:
            raise ValueError("Configuration transport requires certificate verification.")
        host, options = super().build_connection_pool_key_attributes(request, verify, cert)
        # A custom CA must not inherit the default bundle or another caller's roots.
        options["ssl_context"] = _tls_context() if isinstance(verify, str) else self.tls_context
        options["socket_options"] = self.poolmanager.connection_pool_kw["socket_options"]
        return host, options

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        try:
            return super().send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
        except requests.RequestException as exc:
            logger.error(
                "Configuration TLS compatibility request failed: error_type=%s",
                type(exc).__name__,
            )
            raise


def create_appconfig_transport(endpoint: str) -> RequestsTransport:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Configuration transport requires a plain HTTPS endpoint.")
    session = requests.Session()
    session.mount(f"https://{parsed.netloc}/", _TLSCompatibilityAdapter())
    return RequestsTransport(session=session, session_owner=True)
