"""Check that the Packer node-image build writes only a MicroOS image whose checksum openSUSE signed."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "cluster" / "terraform" / "terraform-k8s" / "hcloud-microos-snapshots.pkr.hcl"
)
IMAGE_NAME = "openSUSE-MicroOS.x86_64-ContainerHost-OpenStack-Cloud.qcow2"
# Serves every requested URL from the fixture directory by its last path component, like a mirror would.
FAKE_WGET = """#!/bin/sh
out=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -O) out="$2"; shift 2 ;;
    -*) shift ;;
    *) cp "$FIXTURES/${1##*/}" "${out:-${1##*/}}"; shift ;;
  esac
done
"""


def _download_script(*, pinned_fingerprint: str | None) -> str:
    """Return the template's download step as the shell provisioner runs it."""
    template = TEMPLATE.read_text(encoding="utf-8")
    body = re.search(r"\n  download_image = <<-EOT\n(.*?)\n  EOT\n", template, re.DOTALL)
    fingerprint = re.search(r'\n  opensuse_signing_key_fingerprint = "([0-9A-F]{40})"\n', template)
    assert body is not None
    assert fingerprint is not None
    return textwrap.dedent(body.group(1)).replace(
        "${local.opensuse_signing_key_fingerprint}",
        pinned_fingerprint or fingerprint.group(1),
    )


def _gpg(home: Path, *args: str) -> str:
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is required to sign the test checksum")
@pytest.mark.parametrize("case", ["signed", "tampered_image", "unpinned_signer"])
def test_download_step_refuses_images_without_a_matching_signed_checksum(tmp_path: Path, case: str) -> None:
    """A mirror can swap the image or its signing key, but only the pinned key's checksum lets the build continue."""
    # The signer's keyring and the one the download step creates with mktemp both live here.
    gnupg_homes = tmp_path / "gnupg"
    signer_home = gnupg_homes / "signer"
    signer_home.mkdir(mode=0o700, parents=True)
    fixtures = tmp_path / "mirror"
    fixtures.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    wget = bin_dir / "wget"
    wget.write_text(FAKE_WGET)
    wget.chmod(0o755)
    try:
        _gpg(signer_home, "--quick-gen-key", "Mirror Test <mirror@example.invalid>", "ed25519", "sign", "never")
        listing = _gpg(signer_home, "--with-colons", "--list-keys")
        fingerprint = re.search(r"^fpr:+([0-9A-F]{40}):", listing, re.MULTILINE)
        assert fingerprint is not None
        image = fixtures / IMAGE_NAME
        image.write_bytes(b"genuine image")
        checksum = fixtures / f"{IMAGE_NAME}.sha256"
        checksum.write_text(f"{hashlib.sha256(image.read_bytes()).hexdigest()}  {IMAGE_NAME}\n")
        _gpg(signer_home, "--armor", "--detach-sign", "--output", f"{checksum}.asc", str(checksum))
        (fixtures / "repomd.xml.key").write_text(_gpg(signer_home, "--armor", "--export"))
        if case == "tampered_image":
            image.write_bytes(b"trojaned image")
        script = _download_script(pinned_fingerprint=None if case == "unpinned_signer" else fingerprint.group(1))

        result = subprocess.run(
            ["/bin/sh", "-e", "-c", script],
            cwd=work,
            env={
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "TMPDIR": str(gnupg_homes),
                "FIXTURES": str(fixtures),
                "IMAGE_URL": f"https://download.opensuse.org/tumbleweed/appliances/{IMAGE_NAME}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        for home in gnupg_homes.iterdir():
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=False)

    assert (result.returncode == 0) is (case == "signed"), result.stderr
