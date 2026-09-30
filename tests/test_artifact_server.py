import asyncio
import threading
from pathlib import Path
from typing import cast

from document_export import ArtifactStore

import pytest

from artifact_server import _acquire_store, _store_call


def test_store_call_finishes_blocking_work_before_propagating_repeated_cancellation():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_call():
        started.set()
        release.wait()
        finished.set()
        return "complete"

    async def exercise():
        task = asyncio.create_task(_store_call(blocking_call))
        await asyncio.to_thread(started.wait)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert finished.is_set()

def test_store_call_swallows_background_failure_only_after_parent_cancellation():
    started = threading.Event()
    release = threading.Event()

    def blocking_call():
        started.set()
        release.wait()
        raise RuntimeError("background failure")

    async def exercise():
        task = asyncio.create_task(_store_call(blocking_call))
        await asyncio.to_thread(started.wait)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_cancelled_acquire_releases_lease_after_blocking_acquire_finishes(tmp_path):
    started = threading.Event()
    release_acquire = threading.Event()
    release_finished = threading.Event()
    lease_token = "lease-token"

    class BlockingStore:
        def acquire(self, token, artifact_format):
            started.set()
            release_acquire.wait()
            return Path(tmp_path) / "artifact.pdf", "contract.pdf", lease_token

        def release(self, token):
            assert token == lease_token
            release_finished.set()

    async def exercise():
        task = asyncio.create_task(
            _acquire_store(cast(ArtifactStore, BlockingStore()), "token", "pdf")
        )
        await asyncio.to_thread(started.wait)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert release_finished.is_set()
