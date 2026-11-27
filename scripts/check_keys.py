#!/usr/bin/env python3
"""Check the vendor host keys in this repo.

    check_keys.py                                # check the whole tree
    check_keys.py --base <commit> --author <gh>  # also check a PR against its base

Tree rules (always):
  - keys/ holds only keys/<vendor>/<kid>.pub (plus .gitkeep), and every <vendor>
    has a vendors/<vendor>/submitters.txt.
  - kid is <vendor>-<host>-<yyyy>-<mm>, at most 64 characters. <host> is the
    runner host name the broker will bind to this key ([A-Za-z0-9][A-Za-z0-9.-]{0,39}).
  - Each file is exactly the PEM public key of one Ed25519 key, as
    `jit_client.py keygen` writes it.
  - No public key appears under two kids.
  - vendors/<vendor>/labels.txt, if present, lists that vendor's runner labels:
    one per line, each `<vendor>-...`, lowercase [a-z0-9._-], none of GitHub's
    own labels (self-hosted, linux, x64, ...), no duplicates.

PR rules (--base). Everything a PR is judged against is read from the base
commit, so a PR can't vouch for itself; CI also runs this script from the base.
  - A PR that adds keys changes nothing outside keys/ and vendors/*/labels.txt.
  - The PR author is listed in the base's vendors/<vendor>/submitters.txt for
    every vendor it adds a key for.
  - Existing keys are never modified; rotating means a new kid in a new file.
  - A kid or public key that was ever on the base branch is never added again.
  - A new key's kid is not dated after the current month (UTC), and the key
    has not expired: the broker counts a key's life from the month in its kid
    and stops it KEY_LIFETIME_MONTHS later.
  - Deleting a key is allowed for anyone: it only narrows access.
  - Anyone may propose a labels.txt change; the tree rules above check it.
  CODEOWNERS approval is still required for every PR; these checks don't replace it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Matches the broker: a vendor name is lowercase alphanumeric, no hyphens.
VENDOR_RE = re.compile(r"^[a-z0-9]+$")
# Matches the broker: host as in its _HOST_RE, the whole kid within its 64-char _KID_RE.
HOST_DATE_RE = re.compile(r"^(?P<host>[A-Za-z0-9][A-Za-z0-9.-]{0,39})-(?P<year>20\d\d)-(?P<month>0[1-9]|1[0-2])$")
MAX_KID = 64
# Matches the broker: a key works until the start of the 13th month after its kid's month.
KEY_LIFETIME_MONTHS = 13
LABELS_PATH_RE = re.compile(r"^vendors/[^/]+/labels\.txt$")
# Matches the broker's config: a label is lowercase [a-z0-9._-], starts with
# the vendor's prefix, and is never one of the labels GitHub assigns by itself.
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
RESERVED_LABELS = frozenset({"self-hosted", "linux", "windows", "macos", "x64", "x86", "arm", "arm64"})
KEYGEN_HINT = "generate the pair with `client/jit_client.py keygen` and submit its .pub unchanged"


def parse_kid(vendor: str, kid: str) -> str:
    """Return the host encoded in kid, or raise ValueError."""
    if len(kid) > MAX_KID:
        raise ValueError(f"kid {kid!r} is longer than {MAX_KID} characters")
    if not kid.startswith(f"{vendor}-"):
        raise ValueError(f"kid {kid!r} must start with '{vendor}-'")
    m = HOST_DATE_RE.match(kid[len(vendor) + 1 :])
    if not m:
        raise ValueError(f"kid {kid!r} must be {vendor}-<host>-<yyyy>-<mm>, e.g. {vendor}-host01-2026-11")
    return m["host"]


def kid_expiry(kid: str) -> str:
    """YYYY-MM-DD on which the broker stops accepting this kid (UTC)."""
    year, month = int(kid[-7:-3]), int(kid[-2:])
    months = year * 12 + month - 1 + KEY_LIFETIME_MONTHS
    return f"{months // 12:04d}-{months % 12 + 1:02d}-01"


def check_pem(data: bytes) -> None:
    """Raise ValueError unless data is exactly one Ed25519 public key in canonical PEM."""
    if b"PRIVATE KEY" in data:
        raise ValueError(
            "this is a PRIVATE key. It is now exposed: do not reuse it. Close this PR, "
            "tell sglang, and generate a new pair under a new kid"
        )
    try:
        key = serialization.load_pem_public_key(data)
    except ValueError as e:
        raise ValueError(f"not a PEM public key ({e}); {KEYGEN_HINT}") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"not an Ed25519 key; {KEYGEN_HINT}")
    canonical = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if data != canonical:
        raise ValueError(f"extra text or non-standard formatting around the key; {KEYGEN_HINT}")


def read_submitters(text: str) -> set[str]:
    logins = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            logins.add(line.lower())
    return logins


def check_labels(root: Path) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Return (errors, rows of (vendor, labels)) for every vendors/<vendor>/labels.txt."""
    errors: list[str] = []
    rows: list[tuple[str, list[str]]] = []
    for path in sorted((root / "vendors").glob("*/labels.txt")):
        vendor, rel = path.parent.name, path.relative_to(root).as_posix()
        labels: list[str] = []
        for n, line in enumerate(path.read_text().splitlines(), 1):
            label = line.split("#", 1)[0].strip()
            if not label:
                continue
            if not LABEL_RE.match(label):
                errors.append(f"{rel}:{n}: {label!r} must be lowercase [a-z0-9._-], at most 63 characters")
            elif label in RESERVED_LABELS:
                errors.append(f"{rel}:{n}: {label!r} is a label GitHub assigns by itself")
            elif not label.startswith(f"{vendor}-"):
                errors.append(f"{rel}:{n}: {label!r} must start with '{vendor}-'")
            elif label in labels:
                errors.append(f"{rel}:{n}: {label!r} is listed twice")
            else:
                labels.append(label)
        rows.append((vendor, labels))
    return errors, rows


def check_tree(root: Path) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Return (errors, rows of (path, host, sha256)) for the working tree."""
    errors: list[str] = []
    rows: list[tuple[str, str, str]] = []
    vendors = {p.parent.name for p in (root / "vendors").glob("*/submitters.txt")}
    for v in sorted(vendors):
        if not VENDOR_RE.match(v):
            errors.append(f"vendors/{v}: vendor name must match {VENDOR_RE.pattern}")
    seen: dict[bytes, str] = {}
    keys_dir = root / "keys"
    for path in sorted(p for p in keys_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        parts = path.relative_to(keys_dir).parts
        if parts[-1] == ".gitkeep":
            continue
        if len(parts) != 2 or not parts[1].endswith(".pub"):
            errors.append(f"{rel}: only keys/<vendor>/<kid>.pub files belong under keys/")
            continue
        vendor, kid = parts[0], parts[1][: -len(".pub")]
        if vendor not in vendors:
            errors.append(f"{rel}: unknown vendor {vendor!r} (no vendors/{vendor}/submitters.txt)")
            continue
        data = path.read_bytes()
        try:
            host = parse_kid(vendor, kid)
            check_pem(data)
        except ValueError as e:
            errors.append(f"{rel}: {e}")
            continue
        raw = serialization.load_pem_public_key(data).public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if raw in seen:
            errors.append(f"{rel}: same public key as {seen[raw]}; each host needs its own key pair")
            continue
        seen[raw] = rel
        rows.append((rel, host, hashlib.sha256(data).hexdigest()))
    return errors, rows


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def history_keys(root: Path, base: str) -> tuple[set[str], set[bytes]]:
    """Every kid path and key file content ever added to keys/ up to base."""
    paths: set[str] = set()
    contents: set[bytes] = set()
    commit = None
    for line in git(root, "log", "--diff-filter=A", "--no-renames", "--format=@%H", "--name-only", base, "--", "keys/").splitlines():
        if line.startswith("@"):
            commit = line[1:]
        elif line.endswith(".pub") and commit:
            paths.add(line)
            contents.add(git_bytes(root, "show", f"{commit}:{line}"))
    return paths, contents


def check_pr(root: Path, base: str, author: str, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    errors: list[str] = []
    changes = [
        line.split("\t", 1)
        for line in git(root, "diff", "--name-status", "--no-renames", base, "HEAD").splitlines()
        if line
    ]
    added = [p for s, p in changes if s == "A" and p.startswith("keys/") and not p.endswith("/.gitkeep")]
    for status, path in changes:
        if path.startswith("keys/") and status == "M":
            errors.append(f"{path}: existing keys can't be changed; add a new kid instead (see README, Rotating a key)")
    if added:
        outside = sorted(p for _, p in changes if not p.startswith("keys/") and not LABELS_PATH_RE.match(p))
        if outside:
            errors.append(f"a PR that adds keys may only change keys/ and labels.txt; also changed: {', '.join(outside)}")
    past_paths, past_contents = history_keys(root, base) if added else (set(), set())
    for path in added:
        parts = path.split("/")
        if len(parts) != 3:
            continue  # check_tree reports the layout
        vendor = parts[1]
        try:
            submitters = read_submitters(git(root, "show", f"{base}:vendors/{vendor}/submitters.txt"))
        except subprocess.CalledProcessError:
            errors.append(f"{path}: vendor {vendor!r} is not onboarded on the base branch")
            continue
        if author.lower() not in submitters:
            errors.append(
                f"{path}: @{author} is not in vendors/{vendor}/submitters.txt; "
                f"ask sglang to add your GitHub account first"
            )
        kid = parts[2].removesuffix(".pub")
        try:
            parse_kid(vendor, kid)
        except ValueError:
            pass  # check_tree reports the kid format
        else:
            if kid[-7:] > today[:7]:
                errors.append(f"{path}: kid is dated after {today[:7]}; use the month you generated the key")
            elif kid_expiry(kid) <= today:
                errors.append(f"{path}: a key dated {kid[-7:]} has already expired; generate a new key")
        if path in past_paths:
            errors.append(f"{path}: this kid was used before; kids are never reused, pick a new <yyyy>-<mm>")
        elif (root / path).is_file() and (root / path).read_bytes() in past_contents:
            errors.append(f"{path}: this public key was registered before; generate a new key pair")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="repo checkout (default: .)")
    parser.add_argument("--base", help="base commit of the PR")
    parser.add_argument("--author", help="GitHub login of the PR author (required with --base)")
    args = parser.parse_args()
    if bool(args.base) != bool(args.author):
        parser.error("--base and --author go together")
    root = Path(args.root)

    errors, rows = check_tree(root)
    label_errors, label_rows = check_labels(root)
    errors += label_errors
    if args.base:
        errors += check_pr(root, args.base, args.author)

    table = ["| key | host | expires | sha256 of the .pub file |", "|---|---|---|---|"]
    table += [f"| `{p}` | `{h}` | {kid_expiry(Path(p).stem)} | `{d}` |" for p, h, d in rows]
    labels_table = ["| vendor | labels |", "|---|---|"]
    labels_table += [f"| {v} | {', '.join(f'`{l}`' for l in ls) or '-'} |" for v, ls in label_rows]
    print("\n".join(table) + "\n\n" + "\n".join(labels_table))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("### Registered host keys\n\n" + "\n".join(table) + "\n")
            f.write("\n### Runner labels\n\n" + "\n".join(labels_table) + "\n")
            if errors:
                f.write("\n### Errors\n\n" + "\n".join(f"- {e}" for e in errors) + "\n")
    for e in errors:
        print(f"::error::{e}" if summary else f"error: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
