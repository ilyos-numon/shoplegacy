"""E2E: scripts/backup.sh against the real Postgres, with a matching and a mismatched client."""

import ipaddress
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKER_IMAGE = "postgres:17"


def _server_major(dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    return int(str(row[0])[:2])


def _local_major(tool: str) -> int | None:
    path = shutil.which(tool)
    if path is None:
        return None
    out = subprocess.run([path, "--version"], capture_output=True, text=True, check=False).stdout
    digits = "".join(ch if ch.isdigit() else " " for ch in out).split()
    return int(digits[0]) if digits else None


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _docker_prefix() -> list[str]:
    prefix = ["docker", "run", "--rm", "-i"]
    if sys.platform != "darwin":
        prefix += ["--network", "host"]
    return [*prefix, DOCKER_IMAGE]


def _client(tool: str, dsn: str) -> tuple[list[str], str]:
    """A command prefix for ``tool`` matching the server major, and the DSN to give it."""
    if _local_major(tool) == _server_major(dsn):
        return [tool], dsn
    if sys.platform == "darwin":
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        if host == "localhost" or _is_loopback(host):
            netloc = parts.netloc.rsplit("@", 1)
            netloc[-1] = netloc[-1].replace(host, "host.docker.internal", 1)
            dsn = urlunsplit(
                (parts.scheme, "@".join(netloc), parts.path, parts.query, parts.fragment)
            )
    return [*_docker_prefix(), tool], dsn


def test_backup_script_writes_restorable_dump(pg_dsn: str, tmp_path: Path) -> None:
    dump_cmd, dsn = _client("pg_dump", pg_dsn)
    psql_cmd, _ = _client("psql", pg_dsn)
    restore_cmd, _ = _client("pg_restore", pg_dsn)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(PROJECT_ROOT / "scripts" / "backup.sh", scripts / "backup.sh")
    env = {
        **os.environ,
        "SHOPLEGACY_BACKUP_DSN": dsn,
        "SHOPLEGACY_PG_DUMP": " ".join(dump_cmd),
        "SHOPLEGACY_PSQL": " ".join(psql_cmd),
    }
    done = subprocess.run(
        ["bash", str(scripts / "backup.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    assert done.returncode == 0, done.stderr
    dumps = list((tmp_path / "backups").glob("shoplegacy-*.dump"))
    assert len(dumps) == 1
    assert dumps[0].stat().st_size > 1024
    listing = subprocess.run(
        [*restore_cmd, "--list"],
        input=dumps[0].read_bytes(),
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert listing.returncode == 0, listing.stderr.decode(errors="replace")
    assert "TABLE public shops" in listing.stdout.decode()
    assert not (PROJECT_ROOT / "backups").exists() or not list(
        (PROJECT_ROOT / "backups").glob("*.partial")
    )


def test_backup_script_refuses_mismatched_client(pg_dsn: str, tmp_path: Path) -> None:
    fake = tmp_path / "old_pg_dump"
    fake.write_text('#!/bin/sh\necho "pg_dump (PostgreSQL) 9.6.24"\n')
    fake.chmod(0o755)
    psql_cmd, _ = _client("psql", pg_dsn)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(PROJECT_ROOT / "scripts" / "backup.sh", scripts / "backup.sh")
    env = {
        **os.environ,
        "SHOPLEGACY_BACKUP_DSN": pg_dsn,
        "SHOPLEGACY_PG_DUMP": str(fake),
        "SHOPLEGACY_PSQL": " ".join(psql_cmd),
    }
    if sys.platform == "darwin" and psql_cmd[0] == "docker":
        env["SHOPLEGACY_BACKUP_DSN"] = _client("psql", pg_dsn)[1]
    done = subprocess.run(
        ["bash", str(scripts / "backup.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 3, done.stderr
    assert "pg_dump major 9" in done.stderr
    assert list((tmp_path / "backups").glob("*")) == [] if (tmp_path / "backups").exists() else True
