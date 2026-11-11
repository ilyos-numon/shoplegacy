"""Check the production deploy config. Standard library only.

Usage: uv run python scripts/check_deploy.py --example | --env <file> [--root <dir>]

service.yaml is read with regular expressions (``name:``/``value:``/annotation keys only).
Prints ``DEPLOY_CONFIG_OK`` and exits 0 when everything matches, otherwise prints one
``MISSING ...`` line per problem and exits 1. Without ``--example``/``--env`` prints usage
and exits 2.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEPLOY_FILES = [
    "deploy/README.md",
    "deploy/env.production.example",
    "deploy/gcp/service.yaml",
    "deploy/gcp/setup.sh",
    "deploy/gcp/apply-env.sh",
    "deploy/supabase/PRODUCTION.md",
    "deploy/build-image.sh",
]

# No scheduled-job infrastructure: this file must not exist and these phrases must not appear.
# (The word is split so a repository grep does not match this file itself.)
_SCHED = "sched" + "ule"
FORBIDDEN_FILES = [f"deploy/gcp/{_SCHED}s.sh"]
FORBIDDEN_TEXT: list[tuple[str, re.Pattern[str]]] = [
    ("deploy/gcp/setup.sh", re.compile(f"cloud{_SCHED}r", re.IGNORECASE)),
    ("deploy/README.md", re.compile(rf"{_SCHED}s\.sh|Cloud S{_SCHED[1:]}r")),
    ("deploy/gcp/service.yaml", re.compile(f"S{_SCHED[1:]}r")),
]

REQUIRED_SECRETS = [
    "SHOPLEGACY_DATABASE_URL",
    "SHOPLEGACY_JWT_SECRET",
    "SHOPLEGACY_FEEDBACK_SECRET",
    "SHOPLEGACY_PII_KEYS",
    "SHOPLEGACY_LLM_API_KEY",
    "SHOPLEGACY_TOSS_SECRET_KEY",
]

# Must be in the production env and a plain env in service.yaml.
REQUIRED_ENV = [
    "SHOPLEGACY_PUBLIC_BASE_URL",
    "SHOPLEGACY_PROVIDER",
    "SHOPLEGACY_LLM_BASE_URL",
    "SHOPLEGACY_LLM_MODEL",
    "SHOPLEGACY_LLM_TIMEOUT",
    "SHOPLEGACY_STT_PROVIDER",
    "SHOPLEGACY_STT_WORKER",
    "SHOPLEGACY_STT_MAX_BYTES",
    "SHOPLEGACY_STT_STREAM_MAX_SECONDS",
    "SHOPLEGACY_STT_STREAM_MAX_CONCURRENT",
    "SHOPLEGACY_JWKS_URL",
    "SHOPLEGACY_JWT_AUDIENCE",
    "SHOPLEGACY_SUPABASE_URL",
    "SHOPLEGACY_SUPABASE_ANON_KEY",
    "SHOPLEGACY_TRUSTED_PROXIES",
    "SHOPLEGACY_LOG_FORMAT",
    "SHOPLEGACY_SENTRY_DSN",
    "SHOPLEGACY_REQUIRE_CONSENT",
    "SHOPLEGACY_RATE_LIMIT",
    "SHOPLEGACY_CSP_REPORT_ONLY",
    "SHOPLEGACY_BILLING_PROVIDER",
    "SHOPLEGACY_TOSS_CLIENT_KEY",
    "SHOPLEGACY_SMS_PROVIDER",
    "SHOPLEGACY_NER",
    "SHOPLEGACY_DATA_DIR",
    "SHOPLEGACY_AUDIO_STORE",
    "SHOPLEGACY_SESSION_SECONDS",
    "SHOPLEGACY_SESSIONS_PLANNED",
    "SHOPLEGACY_OWNER_LINK_TTL_HOURS",
    "SHOPLEGACY_REQUIRE_OWNER_APPROVAL",
    "SHOPLEGACY_RETENTION_DAYS",
]

REQUIRED_DEPLOY = [
    "GCP_PROJECT_ID",
    "GCP_REGION",
    "GCP_WIF_PROVIDER",
    "GCP_DEPLOY_SA",
    "CLOUD_RUN_SERVICE",
    "ARTIFACT_REPO",
]

# Keep idle cost near zero: scale to zero instances and no CPU outside requests.
SCALE_RULES = {
    "autoscaling.knative.dev/minScale": "0",
    "run.googleapis.com/cpu-throttling": "true",
}
MAX_SCALE_KEY = "autoscaling.knative.dev/maxScale"
MAX_SCALE_LIMIT = 1

# Plain env values in service.yaml that must be exactly these.
ENV_VALUE_RULES = {
    "SHOPLEGACY_STT_PROVIDER": "fake",
    "SHOPLEGACY_BILLING_PROVIDER": "fake",
    "SHOPLEGACY_SMS_PROVIDER": "log",
    "SHOPLEGACY_STT_WORKER": "thread",
}

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")
_COMMENT_RE = re.compile(r"^\s*#")
_SECTION_RE = re.compile(r"^#\s*===\s*([A-Z]+)\s*$")
_ITEM_RE = re.compile(r"""^\s*-\s+name:\s*["']?([A-Za-z0-9_]+)["']?\s*$""")
_VALUE_RE = re.compile(r"""^\s*value:\s*(?:"([^"]*)"|'([^']*)'|(\S.*?))?\s*$""")
_SECRET_REF_RE = re.compile(r"^\s*secretKeyRef:\s*$")
_REF_NAME_RE = re.compile(r"""^\s*name:\s*["']?([A-Za-z0-9_-]+)["']?\s*$""")
_ANNOTATIONS_RE = re.compile(r"^\s*annotations:\s*$")
_ANNOTATION_RE = re.compile(r"""^\s*([A-Za-z0-9_./-]+):\s*(?:"([^"]*)"|'([^']*)'|(\S.*?))?\s*$""")


def _first(*groups: str | None) -> str:
    for g in groups:
        if g is not None:
            return g
    return ""


def parse_env_keys(text: str) -> list[str]:
    """Keys of a plain .env file, in order, without duplicates."""
    out: list[str] = []
    for line in text.splitlines():
        if _COMMENT_RE.match(line):
            continue
        m = _KEY_RE.match(line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def parse_env_example(text: str) -> dict[str, list[str]]:
    """Keys of env.production.example grouped by ``# === NAME`` section."""
    out: dict[str, list[str]] = {"SECRET": [], "ENV": [], "DEPLOY": [], "UNUSED": []}
    section: str | None = None
    for line in text.splitlines():
        s = _SECTION_RE.match(line)
        if s:
            section = s.group(1)
            out.setdefault(section, [])
            continue
        if _COMMENT_RE.match(line) or section is None:
            continue
        m = _KEY_RE.match(line)
        if m:
            out[section].append(m.group(1))
    return out


def parse_service_env(
    text: str,
) -> tuple[list[str], list[tuple[str, str]], dict[str, str]]:
    """service.yaml env: plain names, (env name, secret name) pairs, plain values."""
    plain: list[str] = []
    secrets: list[tuple[str, str]] = []
    values: dict[str, str] = {}
    current: str | None = None  # most recent `- name: X`
    in_ref = False
    for line in text.splitlines():
        if _COMMENT_RE.match(line):
            continue
        item = _ITEM_RE.match(line)
        if item:
            current = item.group(1)
            in_ref = False
            continue
        val = _VALUE_RE.match(line)
        if current and val:
            plain.append(current)
            values[current] = _first(val.group(1), val.group(2), val.group(3))
            current = None
            continue
        if current and _SECRET_REF_RE.match(line):
            in_ref = True
            continue
        ref = _REF_NAME_RE.match(line)
        if current and in_ref and ref:
            secrets.append((current, ref.group(1)))
            current = None
            in_ref = False
    return plain, secrets, values


def parse_service_annotations(text: str) -> dict[str, str]:
    """``key: value`` lines inside ``annotations:`` blocks. Later keys win."""
    out: dict[str, str] = {}
    indent = -1  # indentation of the `annotations:` line; -1 outside a block
    for line in text.splitlines():
        if _COMMENT_RE.match(line) or not line.strip():
            continue
        lead = len(line) - len(line.lstrip())
        if _ANNOTATIONS_RE.match(line):
            indent = lead
            continue
        if indent < 0:
            continue
        if lead <= indent:
            indent = -1
            continue
        m = _ANNOTATION_RE.match(line)
        if m:
            out[m.group(1)] = _first(m.group(2), m.group(3), m.group(4))
    return out


def _missing(need: list[str], have: list[str]) -> list[str]:
    have_set = set(have)
    return [k for k in need if k not in have_set]


def check_deploy(root: Path, *, example: bool = False, env_file: Path | None = None) -> list[str]:
    """Return the list of problems; empty means OK."""
    problems: list[str] = []
    for rel in DEPLOY_FILES:
        if not (root / rel).exists():
            problems.append(f"missing file: {rel}")
    for rel in FORBIDDEN_FILES:
        if (root / rel).exists():
            problems.append(f"forbidden file: {rel}")
    for rel, pattern in FORBIDDEN_TEXT:
        p = root / rel
        if p.exists() and pattern.search(p.read_text(encoding="utf-8")):
            problems.append(f"forbidden text in {rel}: {pattern.pattern}")

    ex_path = root / "deploy/env.production.example"
    svc_path = root / "deploy/gcp/service.yaml"
    if not ex_path.exists() or not svc_path.exists():
        return problems

    ex = parse_env_example(ex_path.read_text(encoding="utf-8"))
    svc_text = svc_path.read_text(encoding="utf-8")
    plain, secret_refs, values = parse_service_env(svc_text)

    ann = parse_service_annotations(svc_text)
    for name, value in SCALE_RULES.items():
        if ann.get(name) != value:
            problems.append(f"service.yaml annotation {name} must be {value}")
    raw_max = ann.get(MAX_SCALE_KEY, "")
    max_scale = int(raw_max) if re.fullmatch(r"-?\d+", raw_max) else None
    if max_scale is None or max_scale < 1 or max_scale > MAX_SCALE_LIMIT:
        problems.append(f"service.yaml annotation {MAX_SCALE_KEY} must be <= {MAX_SCALE_LIMIT}")
    for name, value in ENV_VALUE_RULES.items():
        if values.get(name) != value:
            problems.append(f"service.yaml env {name} must be {value}")

    for k in _missing(REQUIRED_SECRETS, ex["SECRET"]):
        problems.append(f"env.production.example SECRET section lacks: {k}")
    for k in _missing(ex["SECRET"], REQUIRED_SECRETS):
        problems.append(f"env.production.example SECRET section has unexpected: {k}")

    for env, secret in secret_refs:
        if env != secret:
            problems.append(f"service.yaml secretKeyRef for {env} points at {secret} (must be {env})")
    ref_names = [env for env, secret in secret_refs if env == secret]
    for k in _missing(ex["SECRET"], ref_names):
        problems.append(f"service.yaml lacks secretKeyRef: {k}")
    for k in _missing(ref_names, ex["SECRET"]):
        problems.append(f"service.yaml secretKeyRef not in example SECRET section: {k}")

    for k in _missing(plain, ex["ENV"]):
        problems.append(f"service.yaml env not in example ENV section: {k}")
    for k in _missing(REQUIRED_ENV, plain):
        problems.append(f"service.yaml lacks env: {k}")
    for k in plain:
        if k in REQUIRED_SECRETS:
            problems.append(f"service.yaml has secret as plain env: {k}")

    need = REQUIRED_SECRETS + REQUIRED_ENV
    if example:
        for k in _missing(need, ex["SECRET"] + ex["ENV"]):
            problems.append(f"env.production.example lacks: {k}")
        for k in _missing(REQUIRED_DEPLOY, ex["DEPLOY"]):
            problems.append(f"env.production.example DEPLOY section lacks: {k}")
    if env_file is not None:
        if not env_file.exists():
            problems.append(f"missing env file: {env_file}")
        else:
            keys = parse_env_keys(env_file.read_text(encoding="utf-8"))
            for k in _missing(need, keys):
                problems.append(f"{env_file.name} lacks: {k}")
    return problems


def main(argv: list[str]) -> int:
    example = False
    env_file: Path | None = None
    root = ROOT
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--example":
            example = True
        elif arg == "--env":
            i += 1
            env_file = Path(argv[i]).resolve() if i < len(argv) else None
        elif arg == "--root":
            i += 1
            if i < len(argv):
                root = Path(argv[i]).resolve()
        i += 1
    if not example and env_file is None:
        print(
            "usage: uv run python scripts/check_deploy.py --example | --env <file> [--root <dir>]",
            file=sys.stderr,
        )
        return 2
    problems = check_deploy(root, example=example, env_file=env_file)
    if problems:
        for p in problems:
            print(f"MISSING {p}")
        print(f"deploy config failed: {len(problems)} problem(s)")
        return 1
    print("DEPLOY_CONFIG_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
