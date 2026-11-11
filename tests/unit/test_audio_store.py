from pathlib import Path
from typing import Any

import pytest

from shoplegacy import audio_store as audio_store_module
from shoplegacy.audio_store import FsspecAudioStore, audio_key, build_audio_store
from shoplegacy.config import load_settings
from shoplegacy.crypto import FernetCipher, NullCipher

KEY = "c2hvcGxlZ2FjeS10ZXN0LXBpaS1rZXktMzJieXRlcyE="
BASE_ENV = {
    "SHOPLEGACY_DATABASE_URL": "postgresql://unused@localhost/unused",
    "SHOPLEGACY_JWT_SECRET": "test-secret",
}
AUDIO = "월세가 싸서 들어왔어요".encode()


def _store(tmp_path: Path) -> FsspecAudioStore:
    return FsspecAudioStore(f"file://{tmp_path}/audio", FernetCipher([KEY]))


def test_file_store_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = audio_key("org-1", "job-1", "audio/wav")
    assert key == "org-1/job-1.wav"
    assert not store.exists(key)
    store.put(key, AUDIO, "audio/wav")
    assert store.exists(key)
    assert store.get(key) == AUDIO


def test_stored_bytes_are_not_plaintext(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("org-1/job-1.txt", AUDIO, "text/plain")
    raw = (tmp_path / "audio" / "org-1" / "job-1.txt").read_bytes()
    assert raw != AUDIO
    assert AUDIO not in raw


def test_delete_is_idempotent_and_get_missing_is_key_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("org-1/job-1.ogg", AUDIO, "audio/ogg")
    store.delete("org-1/job-1.ogg")
    store.delete("org-1/job-1.ogg")
    store.delete("org-1/never.ogg")
    assert not store.exists("org-1/job-1.ogg")
    with pytest.raises(KeyError):
        store.get("org-1/job-1.ogg")


def test_rejects_keys_that_escape_the_root(tmp_path: Path) -> None:
    store = FsspecAudioStore(f"file://{tmp_path}/audio", NullCipher())
    for bad in ("../x.wav", "/etc/x.wav", "org//x.wav", ""):
        with pytest.raises(ValueError):
            store.put(bad, AUDIO, "audio/wav")


def test_key_extensions() -> None:
    assert audio_key("o", "j", "audio/x-m4a") == "o/j.m4a"
    assert audio_key("o", "j", "audio/mpeg") == "o/j.mp3"
    assert audio_key("o", "j", "audio/webm") == "o/j.webm"
    assert audio_key("o", "j", "text/plain") == "o/j.txt"


def test_default_store_lives_under_data_dir(tmp_path: Path) -> None:
    settings = load_settings({**BASE_ENV, "SHOPLEGACY_DATA_DIR": str(tmp_path / "data")})
    store = build_audio_store(settings, NullCipher())
    store.put("org-1/job-1.wav", AUDIO, "audio/wav")
    assert (tmp_path / "data" / "audio" / "org-1" / "job-1.wav").read_bytes() == AUDIO


def _capture_url_to_fs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_url_to_fs(url: str, **kwargs: Any) -> tuple[object, str]:
        calls.append((url, kwargs))
        return object(), url.split("://", 1)[1]

    monkeypatch.setattr(audio_store_module.fsspec.core, "url_to_fs", fake_url_to_fs)
    return calls


def test_s3_url_passes_endpoint_as_client_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_url_to_fs(monkeypatch)
    settings = load_settings(
        {
            **BASE_ENV,
            "SHOPLEGACY_AUDIO_STORE": "s3://shop-audio/prefix",
            "SHOPLEGACY_AUDIO_S3_ENDPOINT": "https://storage.example.test/s3",
        }
    )
    build_audio_store(settings, NullCipher())
    assert calls == [
        (
            "s3://shop-audio/prefix",
            {"client_kwargs": {"endpoint_url": "https://storage.example.test/s3"}},
        )
    ]


def test_s3_url_without_endpoint_and_file_url_get_no_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_url_to_fs(monkeypatch)
    FsspecAudioStore("s3://shop-audio", NullCipher())
    FsspecAudioStore("file:///tmp/audio", NullCipher(), "https://ignored.example.test")
    assert calls == [("s3://shop-audio", {}), ("file:///tmp/audio", {})]
