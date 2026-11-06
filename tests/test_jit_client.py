import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "scripts"))
import check_keys  # noqa: E402
import jit_client  # noqa: E402


def test_keygen_output_passes_the_key_check(tmp_path):
    out = tmp_path / "mlu-host01-2026-11"
    jit_client.keygen(str(out))
    key = Path(f"{out}.key")
    pub = Path(f"{out}.pub")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    check_keys.check_pem(pub.read_bytes())  # what keygen writes is what the repo accepts


def test_keygen_refuses_to_overwrite(tmp_path):
    out = tmp_path / "k"
    jit_client.keygen(str(out))
    with pytest.raises(SystemExit):
        jit_client.keygen(str(out))


def test_token_verifies_with_the_public_key(tmp_path):
    out = tmp_path / "k"
    jit_client.keygen(str(out))
    token = jit_client.make_token("mlu", "mlu-host01-2026-11", f"{out}.key")
    assert jwt.get_unverified_header(token)["kid"] == "mlu-host01-2026-11"
    pub = serialization.load_pem_public_key(Path(f"{out}.pub").read_bytes())
    claims = jwt.decode(token, pub, algorithms=["EdDSA"], audience=jit_client.AUDIENCE, issuer="mlu")
    assert claims["exp"] - claims["iat"] == jit_client.TOKEN_LIFETIME
    assert len(claims["jti"]) == 32


def test_runner_loop_requires_its_env():
    r = subprocess.run(["bash", str(ROOT / "client/runner_loop.sh")], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode != 0 and "BROKER_URL" in r.stderr


def loop_env(tmp_path, docker_args):
    keydir = tmp_path / "keys"
    keydir.mkdir()
    (keydir / "k.key").write_text("x")
    client = tmp_path / "fail.py"
    client.write_text("import sys; sys.exit(1)\n")  # every request fails, so the loop only backs off
    return {
        "PATH": os.environ["PATH"],
        "BROKER_URL": "http://127.0.0.1:9",
        "VENDOR": "mlu",
        "KID": "mlu-host01-2026-11",
        "KEY_PATH": str(keydir / "k.key"),
        "RUNNER_LABEL": "mlu-pr",
        "RUNNER_HOST": "host01",
        "RUNNER_IMAGE": "img",
        "CLIENT": str(client),
        "BACKOFF_BASE_SECONDS": "1",
        "DOCKER_ARGS": docker_args.format(keydir=keydir, parent=tmp_path),
    }


@pytest.mark.parametrize(
    "docker_args,message",
    [
        ("--device /dev/x -v {keydir}:/k:ro", "contains the key"),
        ("-v{parent}:/w", "contains the key"),
        ("--mount type=bind,src={keydir},dst=/k", "contains the key"),
        ("--privileged", "--privileged"),
        ("-v /var/run/docker.sock:/var/run/docker.sock", "docker socket"),
    ],
)
def test_runner_loop_refuses_docker_args_exposing_the_key(tmp_path, docker_args, message):
    env = loop_env(tmp_path, docker_args)
    r = subprocess.run(["bash", str(ROOT / "client/runner_loop.sh")], capture_output=True, text=True, env=env, timeout=10)
    assert r.returncode != 0 and message in r.stderr


@pytest.mark.parametrize("docker_args", ["", "--device /dev/* -v /opt/cache:/cache:ro"])
def test_runner_loop_accepts_safe_docker_args(tmp_path, docker_args):
    env = loop_env(tmp_path, docker_args)
    p = subprocess.Popen(["bash", str(ROOT / "client/runner_loop.sh")], stderr=subprocess.PIPE, text=True, env=env)
    try:
        first = p.stderr.readline()  # passed the startup checks and reached the request loop
    finally:
        p.kill()
        p.wait()
    assert "could not get a JIT config" in first


@pytest.mark.parametrize(
    "base,url",
    [
        ("https://broker.example", "https://broker.example/v1/jit/mlu"),
        ("https://broker.example/", "https://broker.example/v1/jit/mlu"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/v1/jit/mlu"),
    ],
)
def test_issue_url(base, url):
    assert jit_client.issue_url(base, "mlu") == url


@pytest.mark.parametrize("base", ["https://broker.example/v1/jit", "broker.example", "https://b.example/?x=1", "ftp://b.example"])
def test_issue_url_wants_the_base_url(base):
    with pytest.raises(SystemExit, match="base URL"):
        jit_client.issue_url(base, "mlu")


def test_issue_url_checks_the_vendor():
    with pytest.raises(SystemExit, match="vendor"):
        jit_client.issue_url("https://broker.example", "../mlu")


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("body,method", [(None, "GET"), ({"labels": ["mlu-pr"], "name": "host01"}, "POST")])
def test_call_signs_each_request(tmp_path, monkeypatch, body, method):
    jit_client.keygen(str(tmp_path / "mlu-host01-2026-11"))
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        return _Resp({"queued": 0})

    monkeypatch.setattr(jit_client.urllib.request, "urlopen", fake_urlopen)
    args = argparse.Namespace(vendor="mlu", kid="mlu-host01-2026-11", key=str(tmp_path / "mlu-host01-2026-11.key"))
    url = jit_client.issue_url("https://broker.example", "mlu") + ("/queue" if body is None else "")
    assert jit_client.call(args, url, body) == {"queued": 0}
    (req,) = seen
    assert (req.get_method(), req.full_url) == (method, url)
    assert (req.data is None) == (body is None)
    token = req.get_header("Authorization").removeprefix("Bearer ")
    assert jwt.get_unverified_header(token)["kid"] == "mlu-host01-2026-11"
