"""Configuration TLS compatibility is explicit, verified and endpoint-scoped."""

import socket
import ssl
from unittest.mock import patch

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection

from connectors.appconfig_transport import create_appconfig_transport


ENDPOINT = "https://config.example.invalid"


def test_compatibility_is_scoped_to_the_configured_https_origin():
    transport = create_appconfig_transport(ENDPOINT)
    adapter = transport.session.get_adapter(ENDPOINT + "/kv")
    other = transport.session.get_adapter("https://another.example.invalid/kv")
    prefix_collision = transport.session.get_adapter(ENDPOINT + ".attacker.invalid/kv")
    try:
        assert type(adapter) is not HTTPAdapter
        assert type(other) is HTTPAdapter
        assert type(prefix_collision) is HTTPAdapter
    finally:
        transport.close()


def test_tls_verification_and_existing_socket_options_are_preserved():
    transport = create_appconfig_transport(ENDPOINT)
    adapter = transport.session.get_adapter(ENDPOINT + "/kv")
    request = requests.Request("GET", ENDPOINT + "/kv").prepare()
    try:
        _, options = adapter.build_connection_pool_key_attributes(request, verify=True)
        context = options["ssl_context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        assert context.maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED
        for option in HTTPConnection.default_socket_options:
            assert option in options["socket_options"]
        assert (socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1200) in options["socket_options"]
    finally:
        transport.close()


def test_custom_ca_bundle_is_preserved():
    transport = create_appconfig_transport(ENDPOINT)
    adapter = transport.session.get_adapter(ENDPOINT + "/kv")
    request = requests.Request("GET", ENDPOINT + "/kv").prepare()
    try:
        _, options = adapter.build_connection_pool_key_attributes(
            request, verify="custom-ca.pem"
        )
        assert options["ca_certs"] == "custom-ca.pem"
        assert options["cert_reqs"] == "CERT_REQUIRED"
        assert options["ssl_context"].cert_store_stats()["x509_ca"] == 0
        _, other_options = adapter.build_connection_pool_key_attributes(
            request, verify="another-custom-ca.pem"
        )
        assert other_options["ssl_context"] is not options["ssl_context"]
    finally:
        transport.close()


def test_disabling_certificate_verification_is_rejected():
    transport = create_appconfig_transport(ENDPOINT)
    adapter = transport.session.get_adapter(ENDPOINT + "/kv")
    request = requests.Request("GET", ENDPOINT + "/kv").prepare()
    try:
        with pytest.raises(ValueError, match="certificate verification"):
            adapter.build_connection_pool_key_attributes(request, verify=False)
    finally:
        transport.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://config.example.invalid",
        "https://user:password@config.example.invalid",
        "https://config.example.invalid/kv",
        "https://config.example.invalid?credential=private",
        "https://config.example.invalid/#fragment",
        "https://",
    ],
)
def test_unsafe_or_non_root_endpoints_are_rejected(endpoint):
    with pytest.raises(ValueError, match="plain HTTPS endpoint"):
        create_appconfig_transport(endpoint)


def test_transport_failures_propagate_without_sensitive_logging(caplog):
    transport = create_appconfig_transport(ENDPOINT)
    adapter = transport.session.get_adapter(ENDPOINT + "/kv")
    request = requests.Request("GET", ENDPOINT + "/kv?private-query-marker=1").prepare()
    error = requests.ReadTimeout("private-exception-marker")
    try:
        with patch.object(HTTPAdapter, "send", side_effect=error):
            with pytest.raises(requests.ReadTimeout) as caught:
                adapter.send(request, timeout=5, verify=True)
        assert caught.value is error
        assert "ReadTimeout" in caplog.text
        assert "private-query-marker" not in caplog.text
        assert "private-exception-marker" not in caplog.text
    finally:
        transport.close()
