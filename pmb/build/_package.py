# Copyright 2023 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import datetime
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pmb.build
import pmb.build.autodetect
import pmb.chroot
import pmb.chroot.apk
import pmb.config.pmaports
import pmb.helpers.package
import pmb.helpers.pmaports
import pmb.helpers.repo
import pmb.parse
import pmb.parse._apkbuild_eval
import pmb.parse.apkindex
from pmb.build.other import BuildStatus
from pmb.core import Chroot
from pmb.core.apk_package import Apkbuild
from pmb.core.apkindex import Apkindex
from pmb.core.arch import Arch
from pmb.core.context import Context, get_context
from pmb.core.pkgrepo import pkgrepo_relative_path
from pmb.helpers import logging
from pmb.helpers.exceptions import BuildFailedError, CommandFailedError, NonBugError
from pmb.types import CrossCompile

from .backend import BootstrapStage, run_abuild


def check_build_for_arch(pkgname: str, arch: Arch) -> bool:
    """
    Check if pmaport can be built or exists as binary for a specific arch.

    :returns: * True when it can be built
              * False when it can't be built, but exists in a binary repo
                (e.g. temp/mesa can't be built for x86_64, but Alpine has it)
    :raises: RuntimeError if the package can't be built for the given arch and
             does not exist as binary package.
    """
    context = get_context()
    # Check for pmaport with arch
    apkbuild = pmb.helpers.pmaports.get(pkgname)
    if arch in Arch.from_arch_field(apkbuild["arch"]):
        return True

    # Check for binary package
    binary = pmb.parse.apkindex.package(pkgname, arch, False)
    if binary:
        pmaport = pmb.helpers.pmaports.get(pkgname)
        pmaport_version = pmaport["pkgver"] + "-r" + pmaport["pkgrel"]
        logging.debug(
            pkgname + ": found pmaport (" + pmaport_version + ") and"
            " binary package (" + binary.version + ", from"
            " postmarketOS or Alpine), but pmaport can't be built"
            f" for {arch} -> using binary package"
        )
        return False

    # No binary package exists and can't build it
    logging.info("NOTE: You can edit the 'arch=' line inside the APKBUILD")
    if context.command == "build":
        logging.info(
            "NOTE: Alternatively, use --arch to build for another"
            " architecture ('pmbootstrap build --arch=armhf " + pkgname + "')"
        )
    raise RuntimeError(f"Can't build '{pkgname}' for architecture {arch}")


def get_depends(context: Context, apkbuild: Apkbuild) -> list[str]:
    """
    Alpine's abuild always builds/installs the "depends" and "makedepends" of a package
    before building it.

    We used to only care about "makedepends"
    and it's still possible to ignore the depends with --ignore-depends.

    :returns: list of dependency pkgnames (eg. ["sdl2", "sdl2_net"])
    """
    # Read makedepends and depends
    if apkbuild["makedepends"]:
        ret = list(apkbuild["makedepends"])
    else:
        ret = list(apkbuild["makedepends_build"]) + list(apkbuild["makedepends_host"])
    if "!check" not in apkbuild["options"]:
        ret += apkbuild["checkdepends"]
    if not context.ignore_depends:
        ret += apkbuild["depends"]
    ret = sorted(set(ret))

    # Don't recurse forever when a package depends on itself (#948)
    for pkgname in [apkbuild["pkgname"], *apkbuild["subpackages"].keys()]:
        if pkgname in ret:
            logging.verbose(apkbuild["pkgname"] + ": ignoring dependency on itself: " + pkgname)
            ret.remove(pkgname)

    # FIXME: is this needed? is this sensible?
    return list(filter(lambda x: not x.startswith("!"), ret))


def get_pkgver(original_pkgver: str, original_source: bool = False) -> str:
    """
    Get the original pkgver when using the original source.

    Otherwise, get the pkgver with an appended suffix of current date and time.
    For example: ``_p20180218550502``
    When appending the suffix, an existing suffix (e.g. ``_git20171231``) gets
    replaced.

    :param original_pkgver: unmodified pkgver from the package's APKBUILD.
    :param original_source: the original source is used instead of overriding
                            it with --src.
    """
    if original_source:
        return original_pkgver

    # Append current date
    no_suffix = original_pkgver.split("_", 1)[0]
    now = datetime.datetime.now()
    new_suffix = "_p" + now.strftime("%Y%m%d%H%M%S")
    return no_suffix + new_suffix


def output_path(arch: Arch, pkgname: str, pkgver: str, pkgrel: str) -> Path:
    # Yeahp, you can just treat an Arch like a path!
    return arch / f"{pkgname}-{pkgver}-r{pkgrel}.apk"


def finish(
    apkbuild: Apkbuild,
    channel: str,
    arch: Arch,
    output: Path,
    chroot: Chroot,
    strict: bool = True,
) -> None:
    """Various finishing tasks that need to be done after a build."""
    # Verify output file
    out_dir = get_context().config.work / "packages" / channel
    if not (out_dir / output).exists():
        raise RuntimeError(f"Package not found after build: {(out_dir / output)}")

    # Clear APKINDEX cache (we only parse APKINDEX files once per session and
    # cache the result for faster dependency resolving, but after we built a
    # package we need to parse it again)
    pmb.parse.apkindex.clear_cache(Apkindex(out_dir / arch / "APKINDEX.tar.gz"))

    # Zap chroots (strict mode)
    if strict:
        logging.info(
            "Zapping buildroots (running in strict mode by default, use --lax to skip zap)"
        )
        pmb.chroot.zap_buildroots()

    logging.info(f"@YELLOW@=>@END@ @BLUE@{channel}/{apkbuild['pkgname']}@END@: Done!")

    # If we just built a package which is used to build other packages, then
    # update the buildroot to use the newly built version.
    if not strict and apkbuild["pkgname"] in pmb.config.build_packages:
        logging.info(
            f"NOTE: Updating package {apkbuild['pkgname']} in buildroot since it's"
            " used for building..."
        )
        pmb.chroot.apk.install([apkbuild["pkgname"]], chroot, build=False, quiet=True)


_package_cache: dict[str, list[str]] = {}


def is_cached_or_cache(arch: Arch, pkgname: str) -> bool:
    """
    Check if a package is in the visited packages cache, if not
    then mark it as visited. We must mark as visited before building
    to break cyclical dependency loops.
    """
    key = str(arch)
    if key not in _package_cache:
        _package_cache[key] = []

    visited = pkgname in _package_cache[key]
    if not visited:
        _package_cache[key].append(pkgname)
    else:
        logging.debug(f"{key}/{pkgname}: marked for build")
    return visited


def get_apkbuild(pkgname: str, arch: Arch | None = None) -> tuple[Path | None, Apkbuild | None]:
    """
    Parse the APKBUILD path for pkgname.

    When there is none, try to find it in the binary package APKINDEX files or raise an exception.

    :param pkgname: package name to be built, as specified in the APKBUILD
    :param arch: architecture the package gets built for. If set, the dependencies are
                 taken from evaluating the APKBUILD in the chroot, see
                 pmb.parse._apkbuild_eval.
    :returns: None or parsed APKBUILD
    """
    # Get pmaport, skip upstream only packages
    pmaport, apkbuild = pmb.helpers.pmaports.get_with_path(pkgname, False)
    if pmaport:
        if arch is not None and apkbuild:
            apkbuild = pmb.parse._apkbuild_eval.refine(apkbuild, pmaport / "APKBUILD", arch)
        pmaport = pkgrepo_relative_path(pmaport)[0]
        return pmaport, apkbuild

    return None, None


@dataclass
class BuildQueueItem:
    name: str
    arch: Arch  # Arch to build for
    aports: str
    apkbuild: Apkbuild
    has_binary: bool  # A binary package exists (even if outdated)
    pkgver: str
    output_path: Path
    channel: str
    depends: list[str]
    cross: CrossCompile


def has_cyclical_dependency(
    unmet_deps: dict[str, list[str]], item: BuildQueueItem, dep: str
) -> bool:
    pkgnames = [item.name, *item.apkbuild["subpackages"].keys()]

    return any(pkgname in unmet_deps.get(dep, []) for pkgname in pkgnames)


def prioritise_build_queue(disarray: list[BuildQueueItem]) -> list[BuildQueueItem]:
    """Figure out The Correct Order to build packages in, or bail."""
    queue: list[BuildQueueItem] = []
    # (name, unmet_dep) of all unmet dependencies
    unmet_deps: dict[str, list[str]] = {}

    # build our base build packages first. This relies on
    # the build_packages array being in the correct order!
    for pkgname in pmb.config.build_packages:
        for item in disarray:
            if item.name == pkgname:
                queue.append(item)
                disarray.remove(item)
                break

    # list of packages in pmaports
    all_pkgnames = set()
    for item in disarray:
        all_pkgnames.add(item.name)
        all_pkgnames.update(item.apkbuild["subpackages"].keys())

    def queue_item(item: BuildQueueItem) -> None:
        queue.append(item)
        disarray.remove(item)
        all_pkgnames.remove(item.name)
        for subpkg in item.apkbuild["subpackages"]:
            all_pkgnames.remove(subpkg)

        unmet_deps.pop(item.name, None)

    stuck = False
    while disarray and not stuck:
        stuck = True
        for item in disarray:
            if not item.depends:
                queue_item(item)
                stuck = False
                break

            # If a dependency hasn't been queued yet, skip until it has been
            missing_deps = False
            for dep in item.depends:
                # This might be a subpkgname, replace with the main pkgname
                # (e.g."linux-pam-dev" -> "linux-pam")
                dep_data = pmb.helpers.package.get(dep, item.arch, must_exist=False)
                if not dep_data:
                    raise NonBugError(f"{item.name}: dependency not found: {dep}")
                dep = dep_data.pkgname

                # If the dependency is a subpackage we can safely ignore it
                if dep in item.apkbuild["subpackages"]:
                    continue

                if dep in all_pkgnames and dep_data.from_pmaports:
                    unmet_deps.setdefault(item.name, []).append(dep)
                    missing_deps = True

                    if has_cyclical_dependency(unmet_deps, item, dep):
                        # If a binary package exists for item, we can queue it
                        # safely and dep will be queued on a future iteration
                        if item.has_binary:
                            logging.warning(
                                f"WARNING: cyclical build dependency: building {item.name} with binary package of {dep}"
                            )
                            queue_item(item)
                            stuck = False
                            break
                        else:
                            logging.warning(
                                f"WARNING: cyclical build dependency: can't build {item.name}, no binary package for {dep}"
                            )
                    else:
                        logging.debug(
                            f"{item.name}: missing dependency {dep}, trying to queue other packages first"
                        )

            if missing_deps:
                continue

            # We're probably good to go??
            queue_item(item)
            stuck = False
            break

    if stuck:
        logging.error("Remaining packages:")
        for unmet_dep in unmet_deps:
            logging.error(f"* {unmet_dep}")
        raise NonBugError("Can't resolve build order of remaining packages!")

    return queue


def process_package(
    context: Context,
    queue_build: Callable,
    pkgname: str,
    arch: Arch | None,
    force: bool,
    from_src: bool,
) -> list[str]:
    """:param arch: Set if we should build for a specific arch."""
    # Only build when APKBUILD exists
    base_aports, base_apkbuild = get_apkbuild(pkgname)
    if not base_apkbuild:
        # We allow this function to be called for packages that aren't in pmaports
        # and just do nothing in this case. However this can be quite confusing
        # when building an Alpine package with --src since we'll just do nothing
        if pmb.parse.apkindex.package(pkgname, arch or Arch.native(), False):
            if from_src:
                raise NonBugError(
                    f"Package {pkgname} is not in pmaports, but exists in Alpine."
                    " to build it with --src you first need to fork it to pmaports."
                    f" Please run 'pmbootstrap aportgen --fork-alpine {pkgname}' and then"
                    " try again"
                )
            return []
        raise RuntimeError(f"{pkgname}: Could not find it in pmaports or any APKINDEX!")

    if arch is None:
        arch = pmb.build.autodetect.arch(base_apkbuild)

    if is_cached_or_cache(arch, pkgname) and not force:
        logging.verbose(f"S{arch}/{pkgname}: already queued")
        return []

    # Now that the arch is known: evaluate the APKBUILD if necessary, so dependencies
    # that are only added for some arches or options are not missed.
    base_aports, base_apkbuild = get_apkbuild(pkgname, arch)
    if not base_apkbuild:
        raise RuntimeError(f"{pkgname}: Could not find it in pmaports or any APKINDEX!")

    logging.debug(f"{arch}/{pkgname}: Generating dependency tree")
    # Add the package to the build queue
    base_depends = get_depends(context, base_apkbuild)

    depends = base_depends.copy()

    base_build_status = BuildStatus.NEW if force else BuildStatus.UNNECESSARY
    if not base_build_status.necessary():
        base_build_status = pmb.build.get_status(arch, base_apkbuild)
    if base_build_status.necessary() and not check_build_for_arch(pkgname, arch):
        base_build_status = BuildStatus.UNNECESSARY

    # FIXME: We descend into the package dependencies even if we aren't going to
    # build it because this is the only way we warn about outdated packages. When
    # you run pmbootstrap install you expect to be warned that you bumped some random
    # utility but forgot to build it.
    # This ought to be fixed, ideally by having the actual dependency parsing stuff
    # here all abstracted away, then the package building code just becomes a couple
    # of callback functions to determine if a package should be built and build it,
    # and we can have another feature to walk the dependency graph and determine if
    # you maybe forgot to build a package, since we only care about that during
    # installation.
    if base_build_status.necessary():
        queue_build(base_aports, base_apkbuild, base_depends)

    parent = pkgname
    while len(depends):
        # FIXME: pop(0) is really quite slow!
        dep = depends.pop(0)
        if is_cached_or_cache(arch, pmb.helpers.package.remove_operators(dep)):
            continue

        # If there is a best provider in the binary repository, we should use
        # that one to satisfy the dependency
        index_data = pmb.parse.apkindex.package(dep, arch, False)
        if index_data:
            dep = index_data.pkgname

        aports, apkbuild = get_apkbuild(dep, arch)
        if not apkbuild:
            continue

        cross = pmb.build.autodetect.crosscompile(apkbuild, arch)
        bstatus = pmb.build.get_status(arch, apkbuild)
        if bstatus.necessary() and dep not in pmb.config.build_packages:
            deps = get_depends(context, apkbuild)
            logging.debug(
                f"BUILDQUEUE: queue {dep} (dependency of {parent}) for build, reason: {bstatus}"
            )
            queue_build(aports, apkbuild, deps, cross)

            logging.verbose(f"{arch}/{dep}: Inserting {len(deps)} dependencies")
            depends = deps + depends
            parent = dep

    return depends


def packages(
    context: Context,
    pkgnames: list[str],
    arch: Arch | None = None,
    force: bool = False,
    strict: bool = True,
    src: str | None = None,
    bootstrap_stage: int = BootstrapStage.NONE,
    log_callback: Callable | None = None,
) -> list[str]:
    """
    Build a package and its dependencies with Alpine Linux' abuild.

    :param pkgname: package name to be built, as specified in the APKBUILD
    :param arch: architecture we're building for (default: autodetected)
    :param force: always build, even if not necessary
    :param strict: avoid building with irrelevant dependencies installed by
                   letting abuild install and uninstall all dependencies.
    :param src: override source used to build the package with a local folder
    :param bootstrap_stage: pass a BOOTSTRAP= env var with the value to abuild
    :param log_callback: function to call before building each package instead of
                         logging. It should accept a single BuildQueueItem parameter.
    :returns: None if the build was not necessary
              output path relative to the packages folder ("armhf/ab-1-r2.apk")
    """
    global _package_cache

    build_queue: list[BuildQueueItem] = []
    built_packages: set[str] = set()

    # We want to build packages in the order they're given here. Due to how we
    # walk the package dependencies, reverse the list so that when we later
    # reverse the build queue we'll be back in the right order.
    pkgnames.reverse()

    # Add a package to the build queue, fetch it's dependency, and
    # add record build helpers to installed (e.g. sccache)
    def queue_build(
        aports: Path,
        apkbuild: Apkbuild,
        depends: list[str],
        cross: CrossCompile | None = None,
    ) -> list[str]:
        # Skip if already queued
        name = apkbuild["pkgname"]
        if any(item.name == name for item in build_queue):
            return []

        pkg_arch = pmb.build.autodetect.arch(apkbuild) if arch is None else arch
        cross = cross or pmb.build.autodetect.crosscompile(apkbuild, pkg_arch)
        pkgver = get_pkgver(apkbuild["pkgver"], src is None)
        channel = pmb.config.pmaports.read_config(aports)["channel"]
        index_data = pmb.parse.apkindex.package(name, pkg_arch, False)
        # Make sure we aren't building a package that will never be used! This can happen if
        # building with --src with an outdated pmaports checkout. Unless --force is used
        # in which case we assume it was intentional.
        if (
            index_data
            and pmb.parse.version.compare(index_data.version, f"{pkgver}-r{apkbuild['pkgrel']}")
            == 1
        ):
            if force:
                logging.warning(
                    f"WARNING: A binary package for {name} has a newer version ({index_data.version})"
                    f" than the source ({pkgver}-r{apkbuild['pkgrel']}). The package to be build will"
                    f" not be installed automatically, use 'apk add {name}={pkgver}-r{apkbuild['pkgrel']}'"
                    " to install it."
                )
            else:
                raise NonBugError(
                    f"A binary package for {name} has a newer version ({index_data.version})"
                    f" than the source ({pkgver}-r{apkbuild['pkgrel']}). Please ensure your pmaports branch is up"
                    " to date and that you don't have a newer version of the package in your local"
                    f" binary repo ({context.config.work / 'packages' / channel / pkg_arch})."
                )
        build_queue.append(
            BuildQueueItem(
                name=name,
                arch=pkg_arch,
                aports=aports.name,  # the pmaports source repo (e.g. "systemd")
                apkbuild=apkbuild,
                has_binary=bool(index_data),
                pkgver=pkgver,
                output_path=output_path(pkg_arch, apkbuild["pkgname"], pkgver, apkbuild["pkgrel"]),
                channel=channel,
                depends=depends,
                cross=cross,
            )
        )

        # If we just queued a package that was request to be built explicitly then
        # record it, since we return which packages we actually built
        if apkbuild["pkgname"] in pkgnames:
            built_packages.add(apkbuild["pkgname"])

        return depends

    if src and len(pkgnames) > 1:
        raise RuntimeError("Can't build multiple packages with --src")

    logging.debug(f"Attempting to build: {', '.join(pkgnames)}")

    # Get existing binary package indexes
    update_arches = set()
    if arch:
        # assume that all the packages are for the provided arch!
        update_arches.add(arch)
    else:
        # Update relevant binary package indexes
        for pkgname in pkgnames:
            _, apkbuild = get_apkbuild(pkgname)
            if apkbuild is None:
                continue  # Let process_package error out for us
            update_arches.add(pmb.build.autodetect.arch(apkbuild))
    for update_arch in update_arches:
        pmb.helpers.repo.update(update_arch)

    # Process the packages we've been asked to build, queuing up any
    # dependencies that need building as well as the package itself
    all_dependencies: list[str] = []
    for pkgname in pkgnames:
        all_dependencies += process_package(
            context, queue_build, pkgname, arch, force, src is not None
        )

    # If any of our common build packages need to be built and are missing, then add them
    # to the queue so they're built first. This is necessary so that our abuild fork is
    # always built first (for example). For now assume that if building in strict mode we
    # should skip this step, but we might want to revisit this later.
    if not src:
        for pkgname in pmb.config.build_packages:
            if pkgname not in pkgnames:
                aport, apkbuild = get_apkbuild(pkgname)
                if not aport or not apkbuild:
                    continue
                build_arch = arch or pmb.build.autodetect.arch(apkbuild)
                aport, apkbuild = get_apkbuild(pkgname, build_arch)
                if not aport or not apkbuild:
                    continue
                bstatus = pmb.build.get_status(build_arch, apkbuild)
                if bstatus.necessary():
                    if strict:
                        raise RuntimeError(
                            f"Strict mode enabled and build package {pkgname} needs building."
                            " Please build it manually first or build without --strict to build"
                            " it automatically."
                        )
                    queue_build(aport, apkbuild, get_depends(context, apkbuild))

    if not len(build_queue):
        return []

    build_queue = prioritise_build_queue(build_queue)

    qlen = len(build_queue)
    logging.info(f"Building @BLUE@{qlen}@END@ package{'s' if qlen > 1 else ''}")
    for item in build_queue:
        logging.info(f"   @BLUE@*@END@ {item.channel}/{item.name}")

    if len(build_queue) > 1:
        if src:
            raise NonBugError(
                "Additional packages need building, please build them first"
                " and then build the package with --src again."
            )
        if context.no_depends:
            raise NonBugError(
                "Additional packages need building, but pmbootstrap was"
                " started with --no-depends. If you see this error in BPO, a"
                " package might be depending on an archived package."
            )

    cross: CrossCompile | None = None
    prev_cross: CrossCompile | None = None
    hostchroot = None  # buildroot for the architecture we're building for

    total_pkgs = len(build_queue)
    for count, pkg in enumerate(build_queue, 1):
        prev_cross = cross
        cross = pkg.cross
        pkg_arch = pkg.arch
        hostchroot = cross.host_chroot(pkg_arch)
        buildchroot = cross.build_chroot(pkg_arch)
        apkbuild = pkg.apkbuild

        channel = pkg.channel
        output = pkg.output_path
        if not log_callback:
            logging.info(
                f"@YELLOW@=> ({count}/{total_pkgs})@END@ @BLUE@{channel}/{pkg.name}@END@: Installing dependencies"
            )
        else:
            log_callback(pkg)

        # FIXME: this is only used to detect special compilers and a workaround for rust
        # in pmb.build.init_compiler(), this should all be refactored and enforce correct
        # APKBUILDs rather than trying to hack things in here
        pkg_depends = list(
            {
                *pkg.depends,
                *apkbuild.get("makedepends", []),
                *apkbuild.get("makedepends_build", []),
                *apkbuild.get("makedepends_host", []),
            }
        )

        # Install the cross compiler into the native chroot. This must happen before the
        # buildroots are initialized: pmb.chroot.apk.install() builds the packages that are
        # missing or outdated (e.g. musl-dev-aarch64 as a dependency of gcc-aarch64), and
        # every finished build zaps all buildroots in strict mode, which would remove the
        # buildroots that we have just initialized.
        init_cross = (strict or cross != prev_cross) and cross.enabled()
        if init_cross:
            pmb.build.init_compiler(context, pkg_depends, cross, pkg_arch)

        # One time chroot initialization
        if hostchroot != buildchroot:
            pmb.build.init(hostchroot)
        if pmb.build.init(buildchroot):
            pmb.build.other.configure_abuild(buildchroot)
            pmb.build.other.configure_ccache(buildchroot)
            if "rust" in all_dependencies or "cargo" in all_dependencies:
                pmb.chroot.apk.install(["sccache"], buildchroot)

        if init_cross and cross == CrossCompile.CROSSDIRECT:
            pmb.chroot.mount_native_into_foreign(buildchroot)

        depends_build: list[str] = []
        depends_host: list[str] = []
        # * makedepends_build are dependencies that should be installed in the native
        #   chroot (e.g. 'meson').
        # * makedepends_host are dependencies that should be
        #   in the chroot for the architecture we're building for (e.g. 'libevdev-dev').
        if apkbuild["makedepends_host"]:
            depends_host = apkbuild["makedepends_host"]
        else:
            depends_host = apkbuild["makedepends"]
        if apkbuild["makedepends_build"]:
            # If we have makedepends_build but not host then just subtract
            # the host depends from the main depends
            if "makedepends_host" not in apkbuild:
                depends_host = list(set(depends_host) - set(apkbuild["makedepends_build"]))
            depends_build = apkbuild["makedepends_build"]
        else:
            depends_build = apkbuild["makedepends"]
            if depends_build and depends_host and cross == CrossCompile.CROSS_NATIVE2:
                logging.warning(
                    "WARNING: makedepends not split into _host and _build variants."
                    " Trying to install all makedepends in both environments, please"
                    " adjust your APKBUILD to specify host dependencies (e.g. libevdev-dev)"
                    " and build dependencies (e.g. meson) separately."
                )
        if "!check" not in apkbuild["options"]:
            depends_build += apkbuild["checkdepends"]

        # If build with --src then we need rsync to copy in the source files.
        if src:
            depends_build.append("rsync")

        if hostchroot == buildchroot:
            pmb.chroot.apk.install(set(depends_host + depends_build), hostchroot, build=False)
        else:
            if depends_host:
                logging.info("*** Install host dependencies")
                pmb.chroot.apk.install(depends_host, hostchroot, build=False)
            if depends_build:
                logging.info("*** Install build dependencies")
                pmb.chroot.apk.install(depends_build, buildchroot, build=False)

        # Build and finish up
        msg = f"@YELLOW@=>@END@ @BLUE@{channel}/{pkg.name}@END@: Building package"
        if cross != CrossCompile.UNNECESSARY:
            msg += f" (cross compiling: {cross})"
        logging.info(msg)

        try:
            run_abuild(
                context,
                apkbuild,
                pkg.pkgver,
                channel,
                pkg_arch,
                cross,
                strict,
                force,
                src,
                bootstrap_stage,
            )
        except CommandFailedError as exception:
            raise BuildFailedError(f"Couldn't build {output}!") from exception
        finish(pkg.apkbuild, channel, pkg_arch, output, buildchroot, strict)

    # Clear package cache for the next run
    _package_cache = {}

    if built_packages:
        logging.info("@YELLOW@=>@END@ @GREEN@Finished building packages")

    return list(built_packages)
