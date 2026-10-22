# Copyright 2026 pmbootstrap contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Dynamic evaluation of APKBUILDs.

pmb.parse._apkbuild is a line based parser. It is fast, but it can not follow
shell control flow, so dependency variables that are changed inside of
"if"/"case" blocks are not seen. For example::

    makedepends="meson"
    if [ "$_use_clang" = 1 ]; then
        makedepends="$makedepends clang"
    fi

So we source the APKBUILD with a real shell inside of the native chroot and read
back the dependency related variables.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Final

import pmb.chroot
from pmb.core import Chroot
from pmb.core.apk_package import Apkbuild
from pmb.core.arch import Arch
from pmb.helpers import logging
from pmb.meta import Cache

# Attributes that are read back from the evaluated APKBUILD. All of them are
# whitespace separated lists, and they all are in pmb.config.apkbuild_attributes.
EVALUATED_ATTRIBUTES: Final[tuple[str, ...]] = (
    "depends",
    "makedepends",
    "makedepends_build",
    "makedepends_host",
    "checkdepends",
    "options",
)

_MARKER_ATTR: Final = "@@PMB-ATTR "
_MARKER_END: Final = "@@PMB-END"
_MARKER_OK: Final = "@@PMB-OK"

# Arguments: $1=CARCH, $2=CBUILD, $3=directory with a copy of the aport.
# Everything the APKBUILD prints while it is sourced is thrown away. Values are
# printed with quoted expansions, so no word splitting or globbing happens in
# the shell, the (multi-line) values are split in python.
_EVAL_SCRIPT: Final = f"""\
CARCH="$1"
CBUILD="$2"
CHOST="$CARCH"
CTARGET="$CARCH"
startdir="$3"
srcdir="$startdir/src"
pkgdir="$startdir/pkg"
subpkgdir="$startdir/subpkg"
cd "$startdir" || exit 1
# Syntax check first, shells differ in how they handle errors in sourced files
"$0" -n ./APKBUILD >/dev/null 2>&1 || exit 1
. ./APKBUILD >/dev/null 2>&1
for _pmb_attr in {" ".join(EVALUATED_ATTRIBUTES)}; do
	eval "_pmb_val=\\"\\${{$_pmb_attr}}\\""
	printf '%s%s\\n%s\\n%s\\n' '{_MARKER_ATTR}' "$_pmb_attr" "$_pmb_val" '{_MARKER_END}'
done
echo '{_MARKER_OK}'
"""


def parse_eval_output(output: str) -> dict[str, list[str]] | None:
    """
    Parse the output of _EVAL_SCRIPT.

    :returns: attribute name -> list of words, or None if the script did not
              run to completion (e.g. syntax error in the APKBUILD)
    """
    ret: dict[str, list[str]] = {}
    attribute: str | None = None
    words: list[str] = []
    ok = False
    for line in output.split("\n"):
        if line.startswith(_MARKER_ATTR):
            attribute = line[len(_MARKER_ATTR) :]
            words = []
        elif line == _MARKER_END:
            if attribute is None:
                return None
            ret[attribute] = words
            attribute = None
        elif line == _MARKER_OK:
            ok = True
        elif attribute is not None:
            words += line.split()

    if not ok or set(ret) != set(EVALUATED_ATTRIBUTES):
        return None
    return ret


@Cache("path", "arch")
def evaluate(path: Path, arch: Arch, chroot: Chroot | None = None) -> dict[str, list[str]] | None:
    """
    Source the APKBUILD in a shell inside of the chroot and return the final
    values of the attributes in EVALUATED_ATTRIBUTES.

    :param path: full path to the APKBUILD file
    :param arch: architecture the package gets built for (-> $CARCH)
    :param chroot: chroot to run the shell in (default: native)
    :returns: attribute name -> list of words, or None if evaluating failed
    """
    chroot = chroot or Chroot.native()
    pmb.chroot.init(chroot)

    # Copy the whole aport (not just the APKBUILD), in case it reads files
    # next to it while it is sourced.
    host_dir = Path(tempfile.mkdtemp(prefix="pmb-apkbuild-eval-", dir=chroot / "tmp"))
    try:
        # mkdtemp() creates the directory with 0700, but the shell runs as
        # a different user inside the chroot
        host_dir.chmod(0o755)
        try:
            shutil.copytree(path.parent, host_dir, symlinks=True, dirs_exist_ok=True)
        except (OSError, shutil.Error) as exception:
            logging.verbose(
                f"Failed to copy {path.parent} for evaluating the APKBUILD: {exception}"
            )
            return None
        chroot_dir = Path("/") / host_dir.relative_to(chroot.path)

        output = pmb.chroot.user(
            ["sh", "-c", _EVAL_SCRIPT, "sh", str(arch), str(Arch.native()), chroot_dir],
            chroot,
            output_return=True,
            check=False,
        )
    finally:
        shutil.rmtree(host_dir, ignore_errors=True)

    return parse_eval_output(output)


def refine(apkbuild: Apkbuild, path: Path, arch: Arch) -> Apkbuild:
    """
    Return the APKBUILD with the attributes in EVALUATED_ATTRIBUTES replaced
    by the values from dynamically evaluating it for the given architecture.
    If evaluating fails, then the passed APKBUILD is returned unchanged.

    :param apkbuild: result of pmb.parse.apkbuild(path)
    :param path: full path to the APKBUILD file
    :param arch: architecture the package gets built for
    """
    values = evaluate(path, arch)
    if values is None:
        logging.warning(
            f"WARNING: {apkbuild['pkgname']}: failed to evaluate the APKBUILD in the chroot,"
            " falling back to the statically parsed dependencies"
        )
        return apkbuild

    ret = dict(apkbuild)
    for attribute, value in values.items():
        if ret.get(attribute) != value:
            added = sorted(set(value) - set(ret.get(attribute) or []))
            removed = sorted(set(ret.get(attribute) or []) - set(value))
            logging.verbose(
                f"{apkbuild['pkgname']}: {attribute} differs after evaluating the APKBUILD for"
                f" {arch}: added {added}, removed {removed}"
            )
        # Copy: the lists in the evaluate() cache must not be shared, callers modify
        # the lists of the returned APKBUILD in place (e.g. depends_build += ...)
        ret[attribute] = list(value)
    return ret
