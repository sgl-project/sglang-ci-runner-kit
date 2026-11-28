import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_kit  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
# PR checks depend on the date (kid month vs now); pin it so the fixed kids below stay valid.
NOW = datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp()


def check_pr(root, base, author):
    return check_kit.check_pr(root, base, author, now=NOW)


def new_pub() -> bytes:
    return Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "vendors/mlu").mkdir(parents=True)
    (tmp_path / "vendors/mlu/submitters.txt").write_text("# comment\nMluBot  # trailing comment\n")
    (tmp_path / "vendors/ppu").mkdir(parents=True)
    (tmp_path / "vendors/ppu/submitters.txt").write_text("ppu-dev\n")
    (tmp_path / "keys/mlu").mkdir(parents=True)
    (tmp_path / "keys/mlu/.gitkeep").write_text("")
    (tmp_path / "keys/mlu/mlu-host01-2026-10.pub").write_bytes(new_pub())
    commit(tmp_path, "base")
    return tmp_path


def commit(root: Path, msg: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


# --- kid and PEM rules ---------------------------------------------------------


@pytest.mark.parametrize(
    "kid,host",
    [("mlu-host01-2026-10", "host01"), ("mlu-gpu-node-3.lab-2027-01", "gpu-node-3.lab")],
)
def test_parse_kid_ok(kid, host):
    assert check_kit.parse_kid("mlu", kid) == host


@pytest.mark.parametrize(
    "kid",
    [
        "ppu-host01-2026-10",  # wrong vendor
        "mlu-host01",  # no date
        "mlu-host01-2026-13",  # bad month
        "mlu-host_01-2026-10",  # underscore not allowed in host
        "mlu--2026-10",  # empty host
        "mlu-" + "h" * 41 + "-2026-10",  # host over 40
        "mlu-" + "h" * 40 + "-" + "x" * 10 + "-2026-10",  # kid over 64
    ],
)
def test_parse_kid_rejects(kid):
    with pytest.raises(ValueError):
        check_kit.parse_kid("mlu", kid)


def test_check_pem_rejects_private_key():
    priv = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(ValueError, match="PRIVATE key"):
        check_kit.check_pem(priv)


def test_check_pem_rejects_other_algorithms_and_extra_text():
    ec = generate_private_key(SECP256R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(ValueError, match="Ed25519"):
        check_kit.check_pem(ec)
    with pytest.raises(ValueError, match="extra text"):
        check_kit.check_pem(b"host01\n" + new_pub())
    with pytest.raises(ValueError, match="not a PEM"):
        check_kit.check_pem(b"ssh-ed25519 AAAAC3Nz... user@host\n")


# --- whole-tree check ------------------------------------------------------------


def test_tree_ok(repo):
    errors, rows = check_kit.check_tree(repo)
    assert errors == []
    assert [(p, h) for p, h, _ in rows] == [("keys/mlu/mlu-host01-2026-10.pub", "host01")]


def test_tree_errors(repo):
    (repo / "keys/mlu/notes.txt").write_text("x")
    (repo / "keys/acme").mkdir()
    (repo / "keys/acme/acme-h-2026-10.pub").write_bytes(new_pub())
    dup = (repo / "keys/mlu/mlu-host01-2026-10.pub").read_bytes()
    (repo / "keys/mlu/mlu-host02-2026-10.pub").write_bytes(dup)
    errors, _ = check_kit.check_tree(repo)
    assert any("notes.txt" in e for e in errors)
    assert any("unknown vendor 'acme'" in e for e in errors)
    assert any("same public key" in e for e in errors)


def test_shipped_tree_passes():
    errors, _ = check_kit.check_tree(REPO)
    assert errors == []
    errors, rows = check_kit.check_labels(REPO)
    assert errors == [] and all(labels for _, labels in rows)


# --- labels.txt ------------------------------------------------------------------


def test_labels_ok(repo):
    (repo / "vendors/mlu/labels.txt").write_text("# lanes\nmlu-1card\nmlu-8card  # big jobs\n\nmlu-nightly-test\n")
    errors, rows = check_kit.check_labels(repo)
    assert errors == []
    assert rows == [("mlu", ["mlu-1card", "mlu-8card", "mlu-nightly-test"])]


def test_labels_errors(repo):
    (repo / "vendors/mlu/labels.txt").write_text("mlu-1card\nppu-1card\nself-hosted\nmlu-PR\nmlu-1card\n")
    errors, rows = check_kit.check_labels(repo)
    assert any(":2: 'ppu-1card' must start with 'mlu-'" in e for e in errors)
    assert any(":3: 'self-hosted' is a label GitHub assigns" in e for e in errors)
    assert any(":4: 'mlu-PR' must be lowercase" in e for e in errors)
    assert any(":5: 'mlu-1card' is listed twice" in e for e in errors)
    assert rows == [("mlu", ["mlu-1card"])]


def test_pr_may_add_keys_and_labels_together(repo):
    base = head(repo)
    (repo / "keys/mlu/mlu-host02-2026-11.pub").write_bytes(new_pub())
    (repo / "vendors/mlu/labels.txt").write_text("mlu-1card\nmlu-2card\n")
    commit(repo, "add key and a lane")
    assert check_pr(repo, base, "mlubot") == []


def test_pr_adding_labels_needs_a_submitter(repo):
    base = head(repo)
    (repo / "vendors/ppu/labels.txt").write_text("ppu-linux-cluster\n")
    commit(repo, "labels")
    assert check_pr(repo, base, "ppu-dev") == []
    errors = check_pr(repo, base, "mlubot")
    assert len(errors) == 1 and "adds ppu-linux-cluster, but @mlubot is not in vendors/ppu/submitters.txt" in errors[0]


def test_pr_removing_labels_by_anyone(repo):
    (repo / "vendors/mlu/labels.txt").write_text("mlu-1card\nmlu-8card\n")
    base = commit(repo, "lanes")
    (repo / "vendors/mlu/labels.txt").write_text("# only one lane now\nmlu-1card\n")
    commit(repo, "drop a lane")
    assert check_pr(repo, base, "someone-else") == []
    (repo / "vendors/mlu/labels.txt").unlink()
    commit(repo, "drop all")
    assert check_pr(repo, base, "someone-else") == []


def test_pr_labels_for_vendor_not_onboarded(repo):
    base = head(repo)
    (repo / "vendors/acme").mkdir()
    (repo / "vendors/acme/labels.txt").write_text("acme-1\n")
    commit(repo, "labels")
    assert any("vendor 'acme' is not onboarded" in e for e in check_pr(repo, base, "acme-dev"))


# --- PR checks -------------------------------------------------------------------


def test_pr_add_by_submitter(repo):
    base = head(repo)
    (repo / "keys/mlu/mlu-host02-2026-11.pub").write_bytes(new_pub())
    commit(repo, "add key")
    assert check_pr(repo, base, "mlubot") == []  # logins match case-insensitively


def test_pr_add_by_non_submitter(repo):
    base = head(repo)
    (repo / "keys/mlu/mlu-host02-2026-11.pub").write_bytes(new_pub())
    commit(repo, "add key")
    errors = check_pr(repo, base, "ppu-dev")
    assert len(errors) == 1 and "not in vendors/mlu/submitters.txt" in errors[0]


def test_pr_cannot_add_itself_to_submitters(repo):
    base = head(repo)
    (repo / "vendors/mlu/submitters.txt").write_text("mlubot\nmallory\n")
    (repo / "keys/mlu/mlu-host02-2026-11.pub").write_bytes(new_pub())
    commit(repo, "sneak")
    errors = check_pr(repo, base, "mallory")
    assert any("may only change keys/" in e for e in errors)
    assert any("@mallory is not in" in e for e in errors)


def test_pr_cannot_modify_key(repo):
    base = head(repo)
    (repo / "keys/mlu/mlu-host01-2026-10.pub").write_bytes(new_pub())
    commit(repo, "swap key")
    errors = check_pr(repo, base, "mlubot")
    assert any("can't be changed" in e for e in errors)


def test_pr_delete_by_anyone(repo):
    base = head(repo)
    (repo / "keys/mlu/mlu-host01-2026-10.pub").unlink()
    commit(repo, "remove key")
    assert check_pr(repo, base, "someone-else") == []


def test_pr_cannot_reuse_kid_or_key(repo):
    old = (repo / "keys/mlu/mlu-host01-2026-10.pub").read_bytes()
    (repo / "keys/mlu/mlu-host01-2026-10.pub").unlink()
    base = commit(repo, "remove key")

    (repo / "keys/mlu/mlu-host01-2026-10.pub").write_bytes(new_pub())
    commit(repo, "re-add kid")
    errors = check_pr(repo, base, "mlubot")
    assert any("kids are never reused" in e for e in errors)

    git(repo, "reset", "-q", "--hard", base)
    (repo / "keys/mlu/mlu-host01-2026-11.pub").write_bytes(old)
    commit(repo, "re-add old key under a new kid")
    errors = check_pr(repo, base, "mlubot")
    assert any("registered before" in e for e in errors)


def test_pr_unknown_vendor_on_base(repo):
    base = head(repo)
    (repo / "keys/acme").mkdir()
    (repo / "keys/acme/acme-h-2026-10.pub").write_bytes(new_pub())
    commit(repo, "add")
    errors = check_pr(repo, base, "mlubot")
    assert any("not onboarded" in e for e in errors)


def test_cli_exit_codes(repo):
    script = REPO / "scripts/check_kit.py"
    ok = subprocess.run([sys.executable, str(script), "--root", str(repo)], capture_output=True, text=True)
    assert ok.returncode == 0 and "host01" in ok.stdout
    base = head(repo)
    (repo / "keys/mlu/mlu-host02-2026-11.pub").write_bytes(new_pub())
    commit(repo, "add")
    bad = subprocess.run(
        [sys.executable, str(script), "--root", str(repo), "--base", base, "--author", "nobody"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1 and "@nobody is not in" in bad.stderr


def test_pr_kid_date(repo):
    base = head(repo)
    (repo / "keys/mlu/mlu-host02-2026-12.pub").write_bytes(new_pub())
    commit(repo, "future")
    errors = check_pr(repo, base, "mlubot")
    assert len(errors) == 1 and "dated after 2026-11" in errors[0]

    git(repo, "reset", "-q", "--hard", base)
    (repo / "keys/mlu/mlu-host02-2025-10.pub").write_bytes(new_pub())
    commit(repo, "expired")
    errors = check_pr(repo, base, "mlubot")
    assert len(errors) == 1 and "already expired" in errors[0]

    git(repo, "reset", "-q", "--hard", base)
    (repo / "keys/mlu/mlu-host02-2025-11.pub").write_bytes(new_pub())  # works until 2026-12-01
    commit(repo, "old but valid")
    assert check_pr(repo, base, "mlubot") == []


def test_kid_expiry():
    assert check_kit.kid_expiry("mlu-host01-2026-11") == "2027-12-01"
    assert check_kit.kid_expiry("mlu-host01-2026-12") == "2028-01-01"
    assert check_kit.kid_expiry("mlu-a-b-2027-01") == "2028-02-01"
