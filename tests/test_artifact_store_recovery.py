import uuid

import pytest

import document_export
from document_export import ArtifactStore


def test_restart_removes_owned_staging_and_pair_paths_but_preserves_unrelated_files(tmp_path):
    (tmp_path / ".docuseal-artifact-store").write_text(
        "docuseal-html-export-v1\n", encoding="utf-8"
    )
    staged = tmp_path / ".stage-interrupted"
    staged.mkdir()
    (staged / "partial").write_bytes(b"partial")
    pair = tmp_path / uuid.uuid4().hex
    pair.mkdir()
    (pair / "document.pdf").write_bytes(b"stale")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    ArtifactStore(tmp_path)

    assert not staged.exists()
    assert not pair.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_restart_rejects_corrupt_store_marker(tmp_path):
    (tmp_path / ".docuseal-artifact-store").write_text("wrong\n", encoding="utf-8")

    with pytest.raises(ValueError, match="marker is invalid"):
        ArtifactStore(tmp_path)


def test_partial_pair_write_is_removed_without_publishing(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    original_write = document_export._write_private

    def fail_docx(path, data):
        if path.name == "document.docx":
            raise OSError("synthetic write failure")
        original_write(path, data)

    monkeypatch.setattr(document_export, "_write_private", fail_docx)

    with pytest.raises(OSError, match="synthetic write failure"):
        store.publish_pair("Contract", b"pdf", b"docx")

    assert store.artifact_count == 0
    assert not tuple(path for path in tmp_path.iterdir() if path.is_dir())


def test_discard_defers_expired_pair_deletion_until_active_download_releases(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    store = ArtifactStore(tmp_path)
    links = store.publish_pair("Contract", b"pdf", b"docx")
    pdf_token = parse_qs(urlsplit(links["pdf_url"]).query)["token"][0]
    docx_token = parse_qs(urlsplit(links["docx_url"]).query)["token"][0]
    lease = store.acquire(pdf_token, "pdf")
    second_lease = store.acquire(pdf_token, "pdf")
    assert lease is not None
    assert second_lease is not None
    pair_path = lease[0].parent

    store.discard_pair(links)

    assert pair_path.is_dir()
    assert store.acquire(docx_token, "docx") is None
    store.release(pdf_token)
    assert store.cleanup_expired() == 0
    store.release(pdf_token)
    assert store.cleanup_expired() == 2
    assert not pair_path.exists()


@pytest.mark.parametrize(
    "options",
    ({"max_bytes": 0}, {"max_files": 0}, {"max_bytes": True}, {"max_files": True}),
)
def test_artifact_store_rejects_invalid_storage_budgets(tmp_path, options):
    with pytest.raises(ValueError, match="budgets must be positive integers"):
        ArtifactStore(tmp_path, **options)


def test_artifact_store_rejects_non_bytes_renderer_output(tmp_path):
    store = ArtifactStore(tmp_path)

    with pytest.raises(ValueError, match="renderers must return bytes"):
        store.publish_pair("Contract", "pdf", b"docx")


def test_discard_ignores_tampered_or_reused_format_links(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    store = ArtifactStore(tmp_path)
    links = store.publish_pair("Contract", b"pdf", b"docx")
    pdf_token = parse_qs(urlsplit(links["pdf_url"]).query)["token"][0]

    wrong_format = dict(links)
    wrong_format["docx_url"] = links["docx_url"].replace("format=docx", "format=pdf")
    store.discard_pair(wrong_format)
    store.discard_pair({**links, "docx_url": links["pdf_url"]})

    assert store.acquire(pdf_token, "pdf") is not None


def test_discard_cleanup_failure_expires_pair_and_logs_safe_error(tmp_path, monkeypatch, caplog):
    from urllib.parse import parse_qs, urlsplit

    caplog.set_level("WARNING", logger="document_export")
    store = ArtifactStore(tmp_path)
    links = store.publish_pair("Contract", b"pdf", b"docx")
    pdf_token = parse_qs(urlsplit(links["pdf_url"]).query)["token"][0]
    lease = store.acquire(pdf_token, "pdf")
    assert lease is not None
    pair_path = lease[0].parent
    store.release(pdf_token)

    def fail_cleanup(path):
        if path == pair_path:
            raise OSError("synthetic cleanup failure")
        raise AssertionError("unexpected cleanup path")

    monkeypatch.setattr(document_export.shutil, "rmtree", fail_cleanup)

    store.discard_pair(links)

    assert store.acquire(pdf_token, "pdf") is None
    assert pair_path.is_dir()
    assert any(record.message == "artifact_cleanup_failed" for record in caplog.records)
