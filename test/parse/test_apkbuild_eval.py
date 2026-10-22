# Copyright 2026 pmbootstrap contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for pmb.parse._apkbuild_eval"""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import pmb.parse._apkbuild
import pmb.parse._apkbuild_eval as apkbuild_eval
from pmb.core import Chroot
from pmb.core.arch import Arch

# Same structure as the systemd APKBUILD: dependencies that get added from
# inside of if/case blocks, which the line based parser does not see.
APKBUILD_CONDITIONAL = """\
pkgname=foo
pkgver=1.0
pkgrel=0
arch="all"
options="!pmb:crossdirect"

_bpf_framework="enabled"
case "$CARCH" in
	riscv64) _bpf_framework="disabled" ;;
esac

makedepends="
	meson
	zlib-dev
	"

# clang: used for bpf compilation
if [ "$_bpf_framework" = enabled ]; then
	makedepends="
		$makedepends
		bpftool
		clang
		"
fi

case "$CARCH" in
	armhf|loongarch64) ;;
	*) makedepends="$makedepends valgrind-dev" ;;
esac

case "$CARCH" in
	aarch64|armv7|x86_64) makedepends="$makedepends xen-dev" ;;
esac

case "$CARCH" in
	*) checkdepends="$checkdepends grep" ;;
esac

build() {
	makedepends="not-seen-here"
}
"""


def _shell() -> str:
    # The APKBUILDs use ${var/foo/bar}, which BusyBox ash (used in the chroot)
    # supports, but not all /bin/sh implementations (e.g. dash).
    for shell in ("busybox", "bash"):
        if path := shutil.which(shell):
            return path
    pytest.skip("need busybox or bash to run the evaluation script")


class FakeChroot:
    """Chroot replacement: a directory on the host, commands run directly on the host."""

    def __init__(self, root: Path) -> None:
        self.path = root
        (root / "tmp").mkdir(parents=True, exist_ok=True)

    def __truediv__(self, other: str) -> Path:
        return self.path / other.strip("/")


@pytest.fixture
def host_chroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Chroot:
    chroot = FakeChroot(tmp_path / "chroot")
    shell = _shell()

    def fake_user(
        cmd: Sequence[str | Path],
        chroot_: Chroot,
        output_return: bool = False,
        check: bool | None = None,
        **_kwargs: object,
    ) -> str:
        # cmd: ["sh", "-c", script, "sh", CARCH, CBUILD, <dir inside chroot>]
        assert list(cmd[:2]) == ["sh", "-c"]
        args = [str(x) for x in cmd[4:]]  # CARCH, CBUILD, directory
        args[-1] = str(chroot_.path) + args[-1]
        # $0 is the shell, like "sh" in the chroot ("$0" -n is used for a syntax check)
        run = [shell, "sh", "-c"] if shell.endswith("busybox") else [shell, "-c"]
        proc = subprocess.run(
            [*run, str(cmd[2]), "sh" if shell.endswith("busybox") else shell, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout

    monkeypatch.setattr("pmb.chroot.init", lambda _chroot: None)
    monkeypatch.setattr("pmb.chroot.user", fake_user)
    apkbuild_eval.evaluate.cache_clear()
    return cast(Chroot, chroot)


def write_aport(tmp_path: Path, content: str, name: str = "foo") -> Path:
    aport = tmp_path / "aports" / name
    aport.mkdir(parents=True)
    path = aport / "APKBUILD"
    path.write_text(content)
    return path


def test_static_parser_misses_conditional_makedepends(tmp_path: Path) -> None:
    """Documents the bug: the line based parser does not see most of the makedepends."""
    path = write_aport(tmp_path, APKBUILD_CONDITIONAL)
    lines = pmb.parse._apkbuild.read_file(path)
    ret = pmb.parse._apkbuild._apkbuild_from_lines(lines, path, check_pkgname=False)
    assert ret["makedepends"] == ["meson", "zlib-dev"]
    assert ret["checkdepends"] == []


@pytest.mark.parametrize(
    "arch, expected_present, expected_absent",
    [
        ("x86_64", {"bpftool", "clang", "valgrind-dev", "xen-dev"}, set()),
        ("aarch64", {"bpftool", "clang", "valgrind-dev", "xen-dev"}, set()),
        ("riscv64", {"valgrind-dev"}, {"bpftool", "clang", "xen-dev"}),
        ("armhf", {"bpftool", "clang"}, {"valgrind-dev", "xen-dev"}),
    ],
)
def test_evaluate(
    host_chroot: Chroot,
    tmp_path: Path,
    arch: str,
    expected_present: set[str],
    expected_absent: set[str],
) -> None:
    path = write_aport(tmp_path, APKBUILD_CONDITIONAL)
    ret = apkbuild_eval.evaluate(path, Arch.from_str(arch), host_chroot)
    assert ret is not None
    makedepends = set(ret["makedepends"])
    assert {"meson", "zlib-dev"} <= makedepends
    assert expected_present <= makedepends
    assert not (expected_absent & makedepends)
    # Set from a case block
    assert ret["checkdepends"] == ["grep"]
    # Functions must not be run
    assert "not-seen-here" not in makedepends
    assert ret["options"] == ["!pmb:crossdirect"]
    # The temporary directory is cleaned up
    assert list((host_chroot.path / "tmp").iterdir()) == []


def test_refine(host_chroot: Chroot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_aport(tmp_path, APKBUILD_CONDITIONAL)
    lines = pmb.parse._apkbuild.read_file(path)
    static = pmb.parse._apkbuild._apkbuild_from_lines(lines, path, check_pkgname=False)

    # refine() runs in the native chroot, use the fake one instead
    monkeypatch.setattr(apkbuild_eval, "Chroot", SimpleNamespace(native=lambda: host_chroot))
    monkeypatch.setattr(apkbuild_eval, "logging", SimpleNamespace(verbose=print, warning=print))
    ret = apkbuild_eval.refine(static, path, Arch.from_str("x86_64"))

    assert "clang" in ret["makedepends"]
    # Original dict is not modified (it may be shared via the parser's cache)
    assert static["makedepends"] == ["meson", "zlib-dev"]
    # Other attributes are unchanged
    assert ret["pkgname"] == "foo"


def test_evaluate_syntax_error_returns_none(host_chroot: Chroot, tmp_path: Path) -> None:
    path = write_aport(tmp_path, 'pkgname=foo\nif [ "$CARCH" = x86_64 ]; then\nmakedepends="a"\n')
    assert apkbuild_eval.evaluate(path, Arch.x86_64, host_chroot) is None


def test_refine_falls_back_to_static(
    host_chroot: Chroot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If evaluating fails, the statically parsed APKBUILD is returned."""
    path = write_aport(tmp_path, APKBUILD_CONDITIONAL)
    lines = pmb.parse._apkbuild.read_file(path)
    static = pmb.parse._apkbuild._apkbuild_from_lines(lines, path, check_pkgname=False)
    path.write_text('if [ "$CARCH" = x86_64 ]; then\n')

    monkeypatch.setattr(apkbuild_eval, "Chroot", SimpleNamespace(native=lambda: host_chroot))
    monkeypatch.setattr(apkbuild_eval, "logging", SimpleNamespace(verbose=print, warning=print))
    assert apkbuild_eval.refine(static, path, Arch.x86_64) is static


def test_parse_eval_output() -> None:
    out = (
        "@@PMB-ATTR depends\na b\nc\n@@PMB-END\n"
        + "".join(
            f"@@PMB-ATTR {x}\n\n@@PMB-END\n"
            for x in apkbuild_eval.EVALUATED_ATTRIBUTES
            if x != "depends"
        )
        + "@@PMB-OK\n"
    )
    ret = apkbuild_eval.parse_eval_output(out)
    assert ret is not None
    assert ret["depends"] == ["a", "b", "c"]
    assert ret["makedepends"] == []
    # Incomplete output
    assert apkbuild_eval.parse_eval_output(out.replace("@@PMB-OK\n", "")) is None
    assert apkbuild_eval.parse_eval_output("") is None
