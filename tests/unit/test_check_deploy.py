"""scripts/check_deploy.py: deploy config checker against tmp fixtures (plan §4.2-4.4)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_deploy.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_deploy", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_deploy"] = mod
    spec.loader.exec_module(mod)
    return mod


cd = _load()

ENV_DEFAULTS = {
    "SHOPLEGACY_STT_PROVIDER": "fake",
    "SHOPLEGACY_BILLING_PROVIDER": "fake",
    "SHOPLEGACY_SMS_PROVIDER": "log",
    "SHOPLEGACY_STT_WORKER": "thread",
    "SHOPLEGACY_LLM_TIMEOUT": "20",
}


def _example_text() -> str:
    lines = ["# === SECRET"]
    lines += [f"{k}=" for k in cd.REQUIRED_SECRETS]
    lines += ["", "# === ENV"]
    lines += [f"{k}=" for k in cd.REQUIRED_ENV]
    lines += ["", "# === DEPLOY"]
    lines += [f"{k}=" for k in cd.REQUIRED_DEPLOY]
    lines += ["", "# === UNUSED", "SHOPLEGACY_SMS_API_KEY="]
    return "\n".join(lines) + "\n"


def _service_text(*, max_scale: str = "1", plain_secret: str | None = None) -> str:
    env: list[str] = []
    for k in cd.REQUIRED_ENV:
        env += [f"            - name: {k}", f'              value: "{ENV_DEFAULTS.get(k, "")}"']
    for k in cd.REQUIRED_SECRETS:
        if k == plain_secret:
            env += [f"            - name: {k}", '              value: "oops"']
            continue
        env += [
            f"            - name: {k}",
            "              valueFrom:",
            "                secretKeyRef:",
            f"                  name: {k}",
            "                  key: latest",
        ]
    head = f"""apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: shoplegacy
  annotations:
    run.googleapis.com/ingress: all
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "0"
        autoscaling.knative.dev/maxScale: "{max_scale}"
        run.googleapis.com/cpu-throttling: "true"
        run.googleapis.com/startup-cpu-boost: "true"
    spec:
      containerConcurrency: 20
      timeoutSeconds: 660
      containers:
        - image: "${{IMAGE}}"
          ports:
            - name: http1
              containerPort: 8830
          env:
"""
    return head + "\n".join(env) + "\n"


def _make_root(tmp_path: Path, *, max_scale: str = "1", plain_secret: str | None = None) -> Path:
    for rel in cd.DEPLOY_FILES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# placeholder\n", encoding="utf-8")
    (tmp_path / "deploy/env.production.example").write_text(_example_text(), encoding="utf-8")
    (tmp_path / "deploy/gcp/service.yaml").write_text(
        _service_text(max_scale=max_scale, plain_secret=plain_secret), encoding="utf-8"
    )
    return tmp_path


def test_valid_config_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_root(tmp_path)
    assert cd.check_deploy(root, example=True) == []
    assert cd.main(["--example", "--root", str(root)]) == 0
    assert "DEPLOY_CONFIG_OK" in capsys.readouterr().out


def test_parsers(tmp_path: Path) -> None:
    text = _service_text()
    plain, secrets, values = cd.parse_service_env(text)
    assert plain == cd.REQUIRED_ENV
    assert secrets == [(k, k) for k in cd.REQUIRED_SECRETS]
    assert values["SHOPLEGACY_STT_WORKER"] == "thread"
    ann = cd.parse_service_annotations(text)
    assert ann["autoscaling.knative.dev/maxScale"] == "1"
    ex = cd.parse_env_example(_example_text())
    assert ex["SECRET"] == cd.REQUIRED_SECRETS
    assert ex["DEPLOY"] == cd.REQUIRED_DEPLOY
    assert cd.parse_env_keys("# A=1\nexport B=2\nC = 3\nB=4\n") == ["B", "C"]


def test_max_scale_two_fails(tmp_path: Path) -> None:
    root = _make_root(tmp_path, max_scale="2")
    problems = cd.check_deploy(root, example=True)
    assert any("maxScale must be <= 1" in p for p in problems)


def test_plain_secret_fails(tmp_path: Path) -> None:
    root = _make_root(tmp_path, plain_secret="SHOPLEGACY_JWT_SECRET")
    problems = cd.check_deploy(root, example=True)
    assert "service.yaml has secret as plain env: SHOPLEGACY_JWT_SECRET" in problems
    assert "service.yaml lacks secretKeyRef: SHOPLEGACY_JWT_SECRET" in problems


def test_env_file_missing_key_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_root(tmp_path)
    keys = [k for k in cd.REQUIRED_SECRETS + cd.REQUIRED_ENV if k != "SHOPLEGACY_LLM_API_KEY"]
    env_file = tmp_path / "prod.env"
    env_file.write_text("".join(f"{k}=x\n" for k in keys), encoding="utf-8")
    assert cd.check_deploy(root, env_file=env_file) == ["prod.env lacks: SHOPLEGACY_LLM_API_KEY"]
    assert cd.main(["--env", str(env_file), "--root", str(root)]) == 1
    assert "MISSING prod.env lacks: SHOPLEGACY_LLM_API_KEY" in capsys.readouterr().out


def test_missing_required_file_fails(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "deploy/gcp/apply-env.sh").unlink()
    assert cd.check_deploy(root, example=True) == ["missing file: deploy/gcp/apply-env.sh"]


def test_forbidden_file_fails(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / cd.FORBIDDEN_FILES[0]).write_text("#!/bin/sh\n", encoding="utf-8")
    assert cd.check_deploy(root, example=True) == [f"forbidden file: {cd.FORBIDDEN_FILES[0]}"]


def test_env_value_rule_fails(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    svc = root / "deploy/gcp/service.yaml"
    svc.write_text(svc.read_text().replace('value: "log"', 'value: "solapi"'), encoding="utf-8")
    assert cd.check_deploy(root) == ["service.yaml env SHOPLEGACY_SMS_PROVIDER must be log"]


def test_no_args_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cd.main([]) == 2
    assert "usage" in capsys.readouterr().err
