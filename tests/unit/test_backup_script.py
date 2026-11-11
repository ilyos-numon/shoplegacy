"""scripts/backup.sh: syntax, missing DSN, and pg_dump/server major mismatch."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _copy_script(root: Path) -> Path:
    """Copy backup.sh into root/scripts so backups/ lands under root, not the repo."""
    scripts = root / "scripts"
    scripts.mkdir()
    target = scripts / "backup.sh"
    shutil.copy(SCRIPT, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return target


def _fake_command(path: Path, output: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\necho '{output}'\n")
    path.chmod(0o755)
    return path


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPLEGACY_")}
    env.update(extra)
    return env


def test_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_missing_dsn_exits_1_with_message(tmp_path: Path) -> None:
    script = _copy_script(tmp_path)
    result = subprocess.run(
        ["bash", str(script)], check=False, capture_output=True, text=True, env=_env()
    )
    assert result.returncode == 1
    assert "SHOPLEGACY_BACKUP_DSN" in result.stderr
    assert not (tmp_path / "backups").exists()


def test_major_mismatch_exits_3_without_writing_a_dump(tmp_path: Path) -> None:
    script = _copy_script(tmp_path)
    fake_dump = _fake_command(tmp_path / "fake_pg_dump", "pg_dump (PostgreSQL) 9.0")
    fake_psql = _fake_command(tmp_path / "fake_psql", "170006")
    backups = tmp_path / "backups"
    before = set(backups.iterdir()) if backups.exists() else set()

    result = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=_env(
            SHOPLEGACY_BACKUP_DSN="postgresql://u:p@localhost:1/db",
            SHOPLEGACY_PG_DUMP=str(fake_dump),
            SHOPLEGACY_PSQL=str(fake_psql),
        ),
    )

    assert result.returncode == 3, result.stderr
    assert "pg_dump major 9 != server 17" in result.stderr
    assert "SHOPLEGACY_PG_DUMP" in result.stderr
    after = set(backups.iterdir()) if backups.exists() else set()
    assert after == before


def test_matching_major_writes_dump_from_stdout(tmp_path: Path) -> None:
    script = _copy_script(tmp_path)
    fake_dump = tmp_path / "fake_pg_dump"
    fake_dump.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "pg_dump (PostgreSQL) 17.6"; exit 0; fi\n'
        "echo DUMPDATA\n"
    )
    fake_dump.chmod(0o755)
    fake_psql = _fake_command(tmp_path / "fake_psql", "170006")

    result = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=_env(
            SHOPLEGACY_BACKUP_DSN="postgresql://u:p@localhost:1/db",
            SHOPLEGACY_PG_DUMP=str(fake_dump),
            SHOPLEGACY_PSQL=str(fake_psql),
        ),
    )

    assert result.returncode == 0, result.stderr
    dumps = list((tmp_path / "backups").glob("shoplegacy-*.dump"))
    assert len(dumps) == 1
    assert dumps[0].read_text() == "DUMPDATA\n"
    assert not list((tmp_path / "backups").glob("*.partial"))
