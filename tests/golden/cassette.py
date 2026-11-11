"""Record/replay of Qwen HTTP traffic for the golden set (vcrpy)."""

from contextlib import AbstractContextManager
from pathlib import Path

import vcr

CASSETTE_DIR = Path(__file__).parent / "cassettes"


def use_cassette(
    case_id: str, *, record: bool, directory: Path = CASSETTE_DIR
) -> AbstractContextManager[None]:
    """Cassette for one golden case; replay never touches the network and never skips."""
    path = directory / f"{case_id}.yaml"
    if not record:
        assert path.is_file(), f"cassette missing: {path}"
    recorder = vcr.VCR(
        record_mode="all" if record else "none",
        match_on=["method", "uri", "body"],
        filter_headers=["authorization"],
        cassette_library_dir=str(directory),
        decode_compressed_response=True,
    )
    return recorder.use_cassette(path.name)  # type: ignore[no-any-return]
