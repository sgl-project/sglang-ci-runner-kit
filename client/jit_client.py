#!/usr/bin/env python3
"""Vendor-side client for the sglang JIT runner broker.

Needs only `PyJWT[crypto]`. One key pair per host: the private key never leaves
the host, and only the .pub file is submitted, by a PR adding
keys/<vendor>/<kid>.pub to sglang-ci-runner-kit.

    # once per host (and again for each rotation); name the files after the kid
    jit_client.py keygen --out /etc/sglang-ci/mlu-host01-2026-11

    # each time a runner is needed; prints the encoded JIT config on stdout.
    # --url is the broker's base URL (BROKER_URL); the client posts to
    # <url>/v1/jit/<vendor>.
    jit_client.py request --url https://<broker> --vendor mlu \
        --kid mlu-host01-2026-11 --key /etc/sglang-ci/mlu-host01-2026-11.key \
        --name host01 --label mlu-pr

    # the vendor's jobs waiting for a runner, as JSON (GET <url>/v1/jit/<vendor>/queue);
    # poll it to size the pool, at most 30 times a minute per key
    jit_client.py queue --url https://<broker> --vendor mlu \
        --kid mlu-host01-2026-11 --key /etc/sglang-ci/mlu-host01-2026-11.key
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

AUDIENCE = "sglang-jit-broker"
TOKEN_LIFETIME = 60


def keygen(out: str) -> None:
    key = Ed25519PrivateKey.generate()
    priv = Path(f"{out}.key")
    pub = Path(f"{out}.pub")
    if priv.exists() or pub.exists():
        sys.exit(f"refusing to overwrite {priv} / {pub}")
    fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    pub.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    print(f"private key: {priv} (keep on this host)\npublic key:  {pub} (submit this as keys/<vendor>/<kid>.pub)")


def make_token(vendor: str, kid: str, key_path: str) -> str:
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    now = int(time.time())
    claims = {
        "iss": vendor,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + TOKEN_LIFETIME,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": kid})


def issue_url(base: str, vendor: str) -> str:
    """<base>/v1/jit/<vendor>. base is BROKER_URL as sglang gives it: scheme and host, no path."""
    url = urllib.parse.urlsplit(base)
    if url.scheme not in ("https", "http") or not url.netloc or url.path not in ("", "/") or url.query:
        sys.exit(f"--url must be the broker's base URL, like https://<broker>, not {base!r}")
    if not re.fullmatch(r"[a-z0-9]+", vendor):
        sys.exit(f"--vendor must be the lowercase vendor name, not {vendor!r}")
    return f"{url.scheme}://{url.netloc}/v1/jit/{vendor}"


def call(args: argparse.Namespace, url: str, body: dict | None = None) -> dict:
    """One signed call to the broker: POST `body` as JSON, or GET when there is none."""
    token = make_token(args.vendor, args.kid, args.key)
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"broker returned {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"broker unreachable: {e.reason}")


def request(args: argparse.Namespace) -> None:
    payload = call(args, issue_url(args.url, args.vendor), {"labels": args.label, "name": args.name})
    print(f"issued runner {payload['runner_name']} (id {payload['runner_id']})", file=sys.stderr)
    print(payload["encoded_jit_config"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="generate this host's Ed25519 key pair")
    p.add_argument("--out", required=True, help="path prefix; writes <out>.key and <out>.pub")

    p = sub.add_parser("token", help="print a signed token (for debugging)")
    p.add_argument("--vendor", required=True)
    p.add_argument("--kid", required=True)
    p.add_argument("--key", required=True)

    p = sub.add_parser("request", help="request a JIT config and print it")
    p.add_argument("--url", required=True, help="the broker's base URL (BROKER_URL), e.g. https://<broker>")
    p.add_argument("--vendor", required=True)
    p.add_argument("--kid", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--name", required=True, help="host name; must match the key's host if it has one")
    p.add_argument("--label", action="append", required=True, help="repeat for several labels")

    p = sub.add_parser("queue", help="print the vendor's jobs waiting for a runner, as JSON")
    p.add_argument("--url", required=True, help="the broker's base URL (BROKER_URL), e.g. https://<broker>")
    p.add_argument("--vendor", required=True)
    p.add_argument("--kid", required=True)
    p.add_argument("--key", required=True)

    args = parser.parse_args()
    if args.cmd == "keygen":
        keygen(args.out)
    elif args.cmd == "token":
        print(make_token(args.vendor, args.kid, args.key))
    elif args.cmd == "queue":
        print(json.dumps(call(args, issue_url(args.url, args.vendor) + "/queue"), indent=2))
    else:
        request(args)


if __name__ == "__main__":
    main()
