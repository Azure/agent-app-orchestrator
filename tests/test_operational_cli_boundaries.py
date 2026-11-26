"""Operational CLI failures must not appear as successful process completion."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from azure.core.exceptions import ServiceRequestError

import upload_prompts
from test_cosmos_failure_boundaries import cosmos_boundary
from util import test_stream


@pytest.mark.parametrize("mode", ["read", "read-os", "write-none", "unexpected", "cancelled", "success", "missing"])
async def test_prompt_upload_reports_failures_and_preserves_confirmed_writes(tmp_path, monkeypatch, caplog, mode):
    marker = "synthetic-private-upload-failure"
    folder = tmp_path / "prompts" / "base"
    if mode != "missing":
        folder.mkdir(parents=True)
        for name in ["first.txt", "middle.txt", "last.txt"]:
            (folder / name).write_text("synthetic prompt", encoding="utf-8")
        if mode == "read":
            (folder / "middle.txt").write_bytes(b"\xff")
        elif mode == "read-os":
            (folder / "middle.txt").unlink()
    monkeypatch.chdir(tmp_path)
    if mode != "missing":
        monkeypatch.setattr(upload_prompts.os, "walk", lambda path: [(str(folder), [], ["first.txt", "middle.txt", "last.txt"])])
    failure = asyncio.CancelledError(marker) if mode == "cancelled" else RuntimeError(marker)
    writes = []

    async def create(container, item_id, *, body):
        assert container == "prompts"
        assert body == {"id": item_id, "content": "synthetic prompt"}
        if item_id == "base_middle":
            if mode in {"unexpected", "cancelled"}:
                raise failure
            if mode == "write-none":
                return None
        writes.append(item_id)
        return body

    client = MagicMock()
    client.create_document = AsyncMock(side_effect=create)
    with (
        patch.object(upload_prompts, "get_config", return_value=MagicMock()) as config,
        patch.object(upload_prompts, "CosmosDBClient", return_value=client),
    ):
        if mode in {"unexpected", "cancelled"}:
            with pytest.raises(type(failure)) as raised:
                await upload_prompts.main()
            assert raised.value is failure
        elif mode != "success":
            with pytest.raises(SystemExit) as raised:
                await upload_prompts.main()
            assert raised.value.code == 1
        else:
            await upload_prompts.main()
    if mode == "missing":
        config.assert_not_called()
        assert writes == []
    elif mode in {"unexpected", "cancelled"}:
        assert writes == ["base_first"]
    elif mode == "success":
        assert writes == ["base_first", "base_middle", "base_last"]
    else:
        assert writes == ["base_first", "base_last"]
        assert caplog.records
    if mode != "missing":
        config.assert_called_once_with()
    assert marker not in caplog.text


@pytest.mark.parametrize("mode", ["sdk", "unexpected", "cancelled", "success"])
async def test_actual_cosmos_result_drives_upload_exit_status(cosmos_boundary, tmp_path, monkeypatch, caplog, mode):
    marker = "synthetic-private-sdk-upload-failure"
    folder = tmp_path / "prompts" / "base"
    folder.mkdir(parents=True)
    (folder / "system.txt").write_text("synthetic prompt", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    client, container = cosmos_boundary
    failure = {
        "sdk": ServiceRequestError(marker),
        "unexpected": RuntimeError(marker),
        "cancelled": asyncio.CancelledError(marker),
    }.get(mode)
    container.create_item = AsyncMock(side_effect=failure, return_value={"id": "base_system"})
    with (
        patch.object(upload_prompts, "get_config", return_value=MagicMock()),
        patch.object(upload_prompts, "CosmosDBClient", return_value=client),
    ):
        if mode == "sdk":
            with pytest.raises(SystemExit) as raised:
                await upload_prompts.main()
            assert raised.value.code == 1
        elif failure:
            with pytest.raises(type(failure)) as raised:
                await upload_prompts.main()
            assert raised.value is failure
        else:
            await upload_prompts.main()
    container.create_item.assert_awaited_once()
    sent = container.create_item.await_args.kwargs["body"]
    assert sent["id"] == "base_system"
    assert sent["content"] == "synthetic prompt"
    assert "lastUpdated" in sent
    assert marker not in caplog.text


@pytest.mark.parametrize("mode", ["connect", "read", "status", "unexpected", "cancelled", "success"])
async def test_stream_cli_preserves_output_cleanup_and_nonzero_failure(mode, caplog, capsys):
    marker = "synthetic-private-stream-failure"
    failure = {
        "connect": httpx.ConnectError(marker),
        "read": httpx.ReadError(marker),
        "unexpected": RuntimeError(marker),
        "cancelled": asyncio.CancelledError(marker),
    }.get(mode)

    class Chunks(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"first"
            if mode in {"read", "unexpected", "cancelled"}:
                raise failure
            yield b"last"

        async def aclose(self):
            self.closed = True

    stream = Chunks()

    def respond(request):
        if mode == "connect":
            raise failure
        return (
            httpx.Response(503, request=request, text=marker)
            if mode == "status" else httpx.Response(200, request=request, stream=stream)
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
    with patch.object(test_stream.httpx, "AsyncClient", return_value=client):
        if mode in {"unexpected", "cancelled"}:
            with pytest.raises(type(failure)) as raised:
                await test_stream.main()
            assert raised.value is failure
        elif mode != "success":
            with pytest.raises(SystemExit) as raised:
                await test_stream.main()
            assert raised.value.code == 1
        else:
            await test_stream.main()
    assert client.is_closed
    output = capsys.readouterr().out
    if mode not in {"connect", "status"}:
        assert stream.closed
        assert "first" in output
    if mode == "success":
        assert "firstlast" in output
    assert marker not in output + caplog.text
