import asyncio
from pathlib import Path
from typing import cast

import pytest

import server
from document_export import RenderLimits
from server import ServerConfig


def config_for(root: Path) -> ServerConfig:
    return ServerConfig(
        artifact_port=9443,
        mcp_host="127.0.0.1",
        artifact_host="0.0.0.0",
        artifact_root=root,
        retention_seconds=3600,
        artifact_max_bytes=4096,
        artifact_max_files=8,
        render_limits=RenderLimits(),
    )


def test_positive_integer_configuration_rejects_boolean_values():
    with pytest.raises(ValueError, match="MAX_FILES"):
        server._positive_int(
            cast(dict[str, str], {"MAX_FILES": True}), "MAX_FILES", 8
        )


def test_render_tool_refuses_use_before_artifact_store_initialization(monkeypatch):
    monkeypatch.setattr(server, "_artifact_store", None)

    with pytest.raises(RuntimeError, match="not initialized"):
        asyncio.run(server.render_template_documents("Contract", "<p>text</p>"))


def test_run_servers_constructs_separate_listeners_and_shuts_them_down(tmp_path, monkeypatch):
    instances = []

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            instances.append(self)

        async def serve(self):
            if self.config.port == 8000:
                return
            while not self.should_exit:
                await asyncio.sleep(0)

    async def wait_for_cleanup(store):
        await asyncio.Event().wait()

    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_SecondaryUvicornServer", FakeServer)
    monkeypatch.setattr(server, "_cleanup_artifacts", wait_for_cleanup)

    asyncio.run(server.run_servers(config_for(tmp_path)))

    assert len(instances) == 2
    mcp_config, artifact_config = (instance.config for instance in instances)
    assert (mcp_config.host, mcp_config.port, mcp_config.access_log) == (
        "127.0.0.1", 8000, False
    )
    assert (artifact_config.host, artifact_config.port, artifact_config.access_log) == (
        "0.0.0.0", 9443, False
    )
    assert all(instance.should_exit for instance in instances)
    assert server._artifact_store is not None
    assert server._artifact_store.root == tmp_path


def test_run_servers_propagates_listener_failure_after_stopping_peer(tmp_path, monkeypatch):
    instances = []

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            instances.append(self)

        async def serve(self):
            if self.config.port == 8000:
                raise RuntimeError("listener failed")
            while not self.should_exit:
                await asyncio.sleep(0)

    async def wait_for_cleanup(store):
        await asyncio.Event().wait()

    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_SecondaryUvicornServer", FakeServer)
    monkeypatch.setattr(server, "_cleanup_artifacts", wait_for_cleanup)

    with pytest.raises(RuntimeError, match="listener failed"):
        asyncio.run(server.run_servers(config_for(tmp_path)))

    assert len(instances) == 2
    assert all(instance.should_exit for instance in instances)


def test_main_loads_config_and_passes_it_to_async_runner(monkeypatch, tmp_path):
    config = config_for(tmp_path)
    async_runs = []
    run_configs = []
    sentinel = object()

    def fake_run_servers(value):
        run_configs.append(value)
        return sentinel

    monkeypatch.setattr(server, "load_config", lambda: config)
    monkeypatch.setattr(server, "run_servers", fake_run_servers)
    monkeypatch.setattr(server.asyncio, "run", async_runs.append)

    server.main()

    assert run_configs == [config]
    assert async_runs == [sentinel]


def test_cleanup_artifacts_logs_expiration_and_stops_on_cancellation(caplog, monkeypatch):
    caplog.set_level("INFO", logger="server")
    cleaned = asyncio.Event()
    sleep_count = 0

    class Store:
        def cleanup_expired(self):
            cleaned.set()
            return 1

    async def controlled_sleep(seconds):
        nonlocal sleep_count
        assert seconds == 60
        sleep_count += 1
        if sleep_count > 1:
            await asyncio.Event().wait()

    async def inline_thread(function, *args):
        return function(*args)

    monkeypatch.setattr(server.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(server.asyncio, "to_thread", inline_thread)

    async def exercise():
        task = asyncio.create_task(server._cleanup_artifacts(Store()))
        await cleaned.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert any(record.message == "artifacts_expired" for record in caplog.records)


def test_module_entrypoint_launches_server_and_secondary_skips_signal_capture(
    monkeypatch,
):
    import runpy

    launched = []

    def capture_coroutine(coroutine):
        launched.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(server.asyncio, "run", capture_coroutine)
    runpy.run_path(server.__file__, run_name="__main__")
    assert len(launched) == 1
    assert launched[0].cr_code.co_name == "run_servers"

    secondary = object.__new__(server._SecondaryUvicornServer)
    with secondary.capture_signals():
        entered = True
    assert entered
