import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

VERSION = "0.3.0"

BASE_DIR = Path(__file__).resolve().parent
# Recipes: one directory per package with package.toml + build script
RECIPES_DIR = BASE_DIR / "recipes"
# Download cache for source archives
SOURCES_DIR = BASE_DIR / "sources"
# Working directories for builds: build/<name>-<version>/{src,pkg}
BUILD_DIR = BASE_DIR / "build"
# Built binary packages: packages/<name>-<version>.tar.xz
PACKAGES_DIR = BASE_DIR / "packages"

# Like pkgsrc's /usr/pkg: rix lives in its own prefix next to the host system.
# Prefix /usr is "system mode" (rix as the package manager of an LFS system).
DEFAULT_PREFIX = "/opt/rix"
SYSTEM_PREFIX = PurePosixPath("/usr")

# Metadata file stored at the top of every binary package
PKGINFO_NAME = ".PKGINFO.json"

# Generic x86-64 so binaries built on one machine also run on an older CPU.
# Never use -march=native here.
DEFAULT_CFLAGS = "-O2 -pipe -march=x86-64-v2"

USE_FLAG_RE = re.compile(r"[a-z0-9][a-z0-9_+-]*")

# Gentoo-style helpers available in every build script. Use them with `if`
# or $(...): a bare `use foo && cmd` as the last line would fail the build.
BUILD_PRELUDE = r'''
use() { [[ " $USE " == *" $1 "* ]]; }
use_enable() { if use "$1"; then echo "--enable-${2:-$1}${3:+=$3}"; else echo "--disable-${2:-$1}"; fi; }
use_with() { if use "$1"; then echo "--with-${2:-$1}${3:+=$3}"; else echo "--without-${2:-$1}"; fi; }
meson_use() { if use "$1"; then echo "-D${2:-$1}=true"; else echo "-D${2:-$1}=false"; fi; }
meson_feature() { if use "$1"; then echo "-D${2:-$1}=enabled"; else echo "-D${2:-$1}=disabled"; fi; }
cmake_use() { if use "$1"; then echo "-D$2=ON"; else echo "-D$2=OFF"; fi; }
. "$RIX_BUILD_SCRIPT"
'''

LOGO = r"""
████  ███ █   █
█   █  █   █ █
████   █    █
█  █   █   █ █
█   █ ███ █   █
"""


class RixError(Exception):
    """An expected error that should be shown to the user without a traceback."""


# ------------------------------------------------------------------- config

@dataclass
class Context:
    """Where packages go and how they are built.

    prefix/sysconfdir/localstatedir are paths as the installed programs see
    them. root is where that system is mounted (normally /); --root is for
    preparing another system, e.g. an image.
    """
    prefix: PurePosixPath
    root: Path
    cflags: str
    cxxflags: str
    ldflags: str
    jobs: int
    use: list       # global USE from the config, e.g. ["ssl", "-doc"]
    packages: dict  # per-package settings from [package.<name>] tables

    @property
    def system_mode(self):
        return self.prefix == SYSTEM_PREFIX

    @property
    def sysconfdir(self):
        return PurePosixPath("/etc") if self.system_mode else self.prefix / "etc"

    @property
    def localstatedir(self):
        return PurePosixPath("/var") if self.system_mode else self.prefix / "var"

    @property
    def allowed_dirs(self):
        """Packages may only install files below these directories."""
        return [self.prefix, self.sysconfdir, self.localstatedir]

    def path(self, target):
        """Map an absolute path on the target system to the real path on disk."""
        return self.root / str(target).lstrip("/")

    @property
    def db_file(self):
        return self.path(self.localstatedir / "db/rix/installed.json")


def config_file():
    if "RIX_CONFIG" in os.environ:
        return Path(os.environ["RIX_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "rix" / "config.toml"


def load_config():
    path = config_file()
    try:
        with open(path, "rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise RixError(f"{path}: {error}")


def check_use_list(entries, where):
    if not isinstance(entries, list):
        raise RixError(f"{where}: 'use' must be a list like [\"ssl\", \"-doc\"]")
    for entry in entries:
        if not isinstance(entry, str) or not USE_FLAG_RE.fullmatch(entry.removeprefix("-")):
            raise RixError(f"{where}: invalid USE flag: {entry!r}")


def check_settings(settings, where):
    """Validate a table of compile settings (global or [package.<name>])."""
    check_use_list(settings.get("use", []), where)
    for key in ("cflags", "cxxflags", "ldflags"):
        if key in settings and not isinstance(settings[key], str):
            raise RixError(f"{where}: '{key}' must be a string")
    if "jobs" in settings and not (isinstance(settings["jobs"], int) and settings["jobs"] > 0):
        raise RixError(f"{where}: 'jobs' must be a positive number")
    env = settings.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise RixError(f"{where}: 'env' must be a table of strings")


def make_context(args):
    """Settings priority: command line > environment > config file > default."""
    config = load_config()
    where = str(config_file())
    check_settings(config, where)
    packages = config.get("package", {})
    if not isinstance(packages, dict):
        raise RixError(f"{where}: 'package' must be a table of [package.<name>] sections")
    for name, settings in packages.items():
        if not isinstance(settings, dict):
            raise RixError(f"{where}: [package.{name}] must be a table")
        check_settings(settings, f"{where} [package.{name}]")

    prefix = PurePosixPath(
        args.prefix or os.environ.get("RIX_PREFIX") or config.get("prefix")
        or DEFAULT_PREFIX
    )
    if not prefix.is_absolute() or ".." in prefix.parts or str(prefix) == "/":
        raise RixError(f"Invalid prefix: {prefix} (must be an absolute directory, not /)")

    root = Path(args.root or os.environ.get("RIX_ROOT") or "/").resolve()
    if not root.is_dir():
        raise RixError(f"Root directory does not exist: {root}")

    cflags = config.get("cflags", DEFAULT_CFLAGS)
    return Context(
        prefix=prefix,
        root=root,
        cflags=cflags,
        cxxflags=config.get("cxxflags", cflags),
        ldflags=config.get("ldflags", ""),
        jobs=config.get("jobs") or os.cpu_count() or 1,
        use=config.get("use", []),
        packages=packages,
    )


def package_settings(name, ctx):
    """Compile settings for one package, like Gentoo's make.conf + package.env.

    Priority: environment (one-off `CFLAGS=-O0 rix build x`) > [package.<name>]
    > global config > default.
    """
    pkg = ctx.packages.get(name, {})
    cflags = os.environ.get("CFLAGS") or pkg.get("cflags") or ctx.cflags
    cxxflags = (os.environ.get("CXXFLAGS") or pkg.get("cxxflags")
                or pkg.get("cflags") or ctx.cxxflags)
    return {
        "cflags": cflags,
        "cxxflags": cxxflags,
        "ldflags": os.environ.get("LDFLAGS") or pkg.get("ldflags") or ctx.ldflags,
        "jobs": pkg.get("jobs") or ctx.jobs,
        "env": pkg.get("env", {}),
    }


def check_writable(ctx):
    """Fail early with a helpful message if packages can't be installed."""
    dest = ctx.path(ctx.prefix)
    if ctx.root != Path("/") and not dest.exists():
        dest.mkdir(parents=True)
    if dest.is_dir() and os.access(dest, os.W_OK):
        return
    if ctx.system_mode:
        raise RixError(f"Cannot write to {dest}, run rix as root")
    user = os.environ.get("USER", "$USER")
    raise RixError(
        f"Cannot write to {dest}. Create it once and give it to your user:\n"
        f"  sudo mkdir -p {dest} && sudo chown {user}: {dest}"
    )


# ------------------------------------------------------- installed database

def load_installed(ctx):
    path = ctx.db_file
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"packages": []}
    except json.JSONDecodeError as error:
        raise RixError(f"{path} is corrupted: {error}")


def save_installed(ctx, db):
    path = ctx.db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    db["packages"].sort(key=lambda p: p["name"])
    # Write to a temp file and rename, so a crash never leaves a half-written DB
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(db, file, indent=2)
        file.write("\n")
    tmp.replace(path)


def find_installed(db, name):
    for package in db["packages"]:
        if package["name"] == name:
            return package
    return None


def is_inside(path, dirs):
    return any(path == d or d in path.parents for d in dirs)


# ------------------------------------------------------------------ recipes

def load_recipe(name):
    recipe_dir = RECIPES_DIR / name
    meta_file = recipe_dir / "package.toml"

    if not meta_file.is_file():
        raise RixError(f"No recipe for package: {name}")

    try:
        with open(meta_file, "rb") as file:
            recipe = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise RixError(f"{meta_file}: {error}")

    for field in ("name", "version"):
        if field not in recipe:
            raise RixError(f"{meta_file}: missing required field '{field}'")

    if recipe["name"] != name:
        raise RixError(
            f"{meta_file}: name '{recipe['name']}' does not match directory '{name}'"
        )

    recipe.setdefault("description", "")
    recipe.setdefault("source", [])
    recipe.setdefault("sha256", [])
    recipe.setdefault("depends", [])       # needed at runtime (and to build)
    recipe.setdefault("makedepends", [])   # needed only to build

    for field in ("source", "sha256", "depends", "makedepends"):
        if not isinstance(recipe[field], list):
            raise RixError(f"{meta_file}: '{field}' must be a list")

    if len(recipe["source"]) != len(recipe["sha256"]):
        raise RixError(
            f"{meta_file}: 'source' and 'sha256' must have the same number of entries"
        )

    load_recipe_use(recipe, meta_file)
    recipe["dir"] = recipe_dir
    return recipe


def load_recipe_use(recipe, meta_file):
    """Normalize the [use] table.

    A flag is either `ssl = true` or `ssl = { default = true, description = "..." }`.
    [use_depends] / [use_makedepends] map a flag to extra dependencies.
    """
    use_table = recipe.get("use", {})
    if not isinstance(use_table, dict):
        raise RixError(f"{meta_file}: 'use' must be a table of flag = true/false")

    defaults, descriptions = {}, {}
    for flag, value in use_table.items():
        if not USE_FLAG_RE.fullmatch(flag):
            raise RixError(f"{meta_file}: invalid USE flag name: {flag!r}")
        if isinstance(value, dict):
            descriptions[flag] = value.get("description", "")
            value = value.get("default", False)
        if not isinstance(value, bool):
            raise RixError(f"{meta_file}: USE flag '{flag}' default must be true or false")
        defaults[flag] = value
    recipe["use"] = defaults
    recipe["use_description"] = descriptions

    for field in ("use_depends", "use_makedepends"):
        table = recipe.setdefault(field, {})
        if not isinstance(table, dict):
            raise RixError(f"{meta_file}: '{field}' must be a table of flag = [packages]")
        for flag, deps in table.items():
            if flag not in defaults:
                raise RixError(f"{meta_file}: [{field}] uses unknown flag '{flag}'")
            if not isinstance(deps, list):
                raise RixError(f"{meta_file}: [{field}] {flag} must be a list")


_warned = set()


def effective_use(recipe, ctx):
    """Gentoo-style USE resolution: recipe default < global use < [package.X] use.

    Returns {flag: (enabled, source)}. Global flags a recipe doesn't have are
    ignored (that's normal); unknown flags in [package.X] are warned about.
    """
    flags = {flag: (enabled, "default") for flag, enabled in recipe["use"].items()}
    name = recipe["name"]
    layers = [
        ("global", ctx.use),
        ("package", ctx.packages.get(name, {}).get("use", [])),
    ]
    for source, entries in layers:
        for entry in entries:
            enabled = not entry.startswith("-")
            flag = entry.removeprefix("-")
            if flag in flags:
                flags[flag] = (enabled, source)
            elif source == "package" and (name, flag) not in _warned:
                _warned.add((name, flag))
                print(f"warning: [package.{name}] sets unknown USE flag '{flag}'",
                      file=sys.stderr)
    return flags


def enabled_use(recipe, ctx):
    return sorted(f for f, (enabled, _) in effective_use(recipe, ctx).items() if enabled)


def format_use(recipe, ctx):
    """'nls -static' style summary, like emerge -v."""
    flags = effective_use(recipe, ctx)
    return " ".join(f if on else f"-{f}" for f, (on, _) in sorted(flags.items()))


def load_repo():
    """Return all recipes; broken ones are reported and skipped."""
    if not RECIPES_DIR.is_dir():
        return []

    recipes = []
    for recipe_dir in sorted(RECIPES_DIR.iterdir()):
        if not (recipe_dir / "package.toml").is_file():
            continue
        try:
            recipes.append(load_recipe(recipe_dir.name))
        except RixError as error:
            print(f"warning: {error}", file=sys.stderr)
    return recipes


def archive_path(recipe, ctx):
    """Packages only work in the prefix they were built for, so each prefix
    gets its own directory: packages/opt-rix/zlib-1.3.1.tar.xz"""
    prefix_dir = str(ctx.prefix).strip("/").replace("/", "-")
    return PACKAGES_DIR / prefix_dir / f"{recipe['name']}-{recipe['version']}.tar.xz"


def runtime_deps(recipe, ctx):
    deps = list(recipe["depends"])
    for flag in enabled_use(recipe, ctx):
        deps += recipe["use_depends"].get(flag, [])
    return list(dict.fromkeys(deps))  # dedupe, keep order


def build_deps(recipe, ctx):
    deps = runtime_deps(recipe, ctx) + recipe["makedepends"]
    for flag in enabled_use(recipe, ctx):
        deps += recipe["use_makedepends"].get(flag, [])
    return list(dict.fromkeys(deps))


def resolve(names, deps_of, ctx):
    """Return recipes for names and all their dependencies, dependencies first.

    deps_of(recipe, ctx) chooses which edges to follow (build or runtime deps);
    USE flags can add dependencies, hence ctx.
    A depth-first walk gives a topological order; a package seen again while
    it is still being visited means a dependency cycle.
    """
    order = []
    state = {}  # name -> "visiting" | "done"

    def visit(name, path):
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            cycle = path[path.index(name):] + [name]
            raise RixError("Dependency cycle: " + " -> ".join(cycle))

        try:
            recipe = load_recipe(name)
        except RixError:
            if path:
                raise RixError(f"{path[-1]} depends on {name}, but there is no recipe for it")
            raise

        state[name] = "visiting"
        for dep in deps_of(recipe, ctx):
            visit(dep, path + [name])
        state[name] = "done"
        order.append(recipe)

    for name in names:
        visit(name, [])
    return order


# ------------------------------------------------------------------ sources

def sha256_of(path):
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def check_sha256(path, expected):
    actual = sha256_of(path)
    if actual != expected:
        raise RixError(
            f"Checksum mismatch for {path.name}\n"
            f"  expected: {expected}\n"
            f"  got:      {actual}"
        )


def fetch_source(recipe, source, expected):
    """Return a local path to a verified source file.

    A source with '://' is downloaded into the cache; anything else is a file
    next to the recipe (e.g. a patch).
    """
    if "://" not in source:
        path = recipe["dir"] / source
        if not path.is_file():
            raise RixError(f"Local source not found: {path}")
        check_sha256(path, expected)
        return path

    SOURCES_DIR.mkdir(exist_ok=True)
    filename = source.rstrip("/").rsplit("/", 1)[-1]
    dest = SOURCES_DIR / filename

    if dest.is_file() and sha256_of(dest) == expected:
        print(f"==> Using cached {filename}")
        return dest

    print(f"==> Downloading {source}")
    partial = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(source, timeout=60) as response, \
                open(partial, "wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, OSError) as error:
        partial.unlink(missing_ok=True)
        raise RixError(f"Download failed: {source}: {error}")

    try:
        check_sha256(partial, expected)
    except RixError:
        partial.unlink()
        raise

    partial.replace(dest)
    return dest


def unpack(path, dest):
    """Extract an archive into dest, or copy a plain file (patch etc.)."""
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tar:
            # "data" filter blocks absolute paths and ../ escapes
            tar.extractall(dest, filter="data")
    else:
        shutil.copy2(path, dest)


# -------------------------------------------------------------------- build

def build_env(recipe, ctx, pkg_dir, src_dir):
    """Environment for build scripts: find dependencies in the rix prefix first."""
    env = os.environ.copy()
    prefix = str(ctx.prefix)
    settings = package_settings(recipe["name"], ctx)

    env["PKG"] = str(pkg_dir)
    env["SRC"] = str(src_dir)
    env["NAME"] = recipe["name"]
    env["VERSION"] = recipe["version"]
    env["PREFIX"] = prefix
    env["SYSCONFDIR"] = str(ctx.sysconfdir)
    env["LOCALSTATEDIR"] = str(ctx.localstatedir)
    env["USE"] = " ".join(enabled_use(recipe, ctx))
    env["CFLAGS"] = settings["cflags"]
    env["CXXFLAGS"] = settings["cxxflags"]
    env["LDFLAGS"] = settings["ldflags"]
    env["MAKEFLAGS"] = os.environ.get("MAKEFLAGS") or f"-j{settings['jobs']}"
    # Extra per-package variables from [package.<name>.env] go last
    env.update(settings["env"])

    if ctx.system_mode:
        return env

    # Where the prefix really is on disk (differs from prefix only with --root)
    on_disk = str(ctx.path(ctx.prefix))

    def prepend(var, *values, sep=os.pathsep):
        parts = list(values)
        if env.get(var):
            parts.append(env[var])
        env[var] = sep.join(parts)

    prepend("PATH", f"{on_disk}/bin", f"{on_disk}/sbin")
    prepend("PKG_CONFIG_PATH", f"{on_disk}/lib/pkgconfig", f"{on_disk}/share/pkgconfig")
    prepend("CMAKE_PREFIX_PATH", on_disk)
    prepend("ACLOCAL_PATH", f"{on_disk}/share/aclocal")
    prepend("CPPFLAGS", f"-I{on_disk}/include", sep=" ")
    # rpath uses the real prefix: that's where libraries are found at runtime,
    # so programs work without LD_LIBRARY_PATH.
    prepend("LDFLAGS", f"-L{on_disk}/lib", f"-Wl,-rpath,{prefix}/lib", sep=" ")
    if ctx.root != Path("/"):
        # .pc files contain the real prefix; make pkg-config look under root
        env["PKG_CONFIG_SYSROOT_DIR"] = str(ctx.root)
    return env


def run_logged(command, cwd, env, log_path):
    """Run a command, streaming output to the terminal and a log file."""
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for line in process.stdout:
            sys.stdout.write(line)
            log.write(line)
        return process.wait()


def list_files(pkg_dir):
    """Paths that the package installs, as absolute paths on the target."""
    files = []
    for path in pkg_dir.rglob("*"):
        if path.is_symlink() or not path.is_dir():
            files.append("/" + str(path.relative_to(pkg_dir)))
    return sorted(files)


def as_root(tarinfo):
    """Packages are built as a normal user, but in system mode files must
    belong to root. (Installing as a normal user ignores this.)"""
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = "root"
    return tarinfo


def create_package(recipe, ctx, pkg_dir, files):
    """Pack pkg_dir into packages/<name>-<version>.tar.xz with metadata."""
    archive = archive_path(recipe, ctx)
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_name(archive.name + ".part")

    pkginfo = {
        "name": recipe["name"],
        "version": recipe["version"],
        "description": recipe["description"],
        "depends": runtime_deps(recipe, ctx),
        "use": enabled_use(recipe, ctx),
        "prefix": str(ctx.prefix),
        "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": files,
    }
    pkginfo_bytes = json.dumps(pkginfo, indent=2).encode("utf-8")

    with tarfile.open(partial, "w:xz") as tar:
        info = as_root(tarfile.TarInfo(PKGINFO_NAME))
        info.size = len(pkginfo_bytes)
        info.mtime = int(time.time())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(pkginfo_bytes))

        # Sorted so parent directories always come before their contents
        for path in sorted(pkg_dir.rglob("*")):
            arcname = str(path.relative_to(pkg_dir))
            tar.add(path, arcname=arcname, recursive=False, filter=as_root)

    partial.replace(archive)
    return archive


def build_package(recipe, ctx):
    """Build one recipe into a binary package. Dependencies must be installed."""
    name = recipe["name"]
    build_script = recipe["dir"] / "build"
    if not build_script.is_file():
        raise RixError(f"Recipe {name} has no build script")

    full_name = f"{name}-{recipe['version']}"
    work_dir = BUILD_DIR / full_name
    src_dir = work_dir / "src"
    pkg_dir = work_dir / "pkg"

    print(f"==> Building {full_name} for {ctx.prefix}")
    use_summary = format_use(recipe, ctx)
    if use_summary:
        print(f"    USE: {use_summary}")

    # Always start from a clean tree
    if work_dir.exists():
        shutil.rmtree(work_dir)
    src_dir.mkdir(parents=True)
    pkg_dir.mkdir()

    # The build runs inside the top-level directory of the first archive
    # (e.g. src/zlib-1.3.1); other sources end up next to it in $SRC.
    build_cwd = src_dir
    for index, (source, checksum) in enumerate(zip(recipe["source"], recipe["sha256"])):
        path = fetch_source(recipe, source, checksum)
        print(f"==> Unpacking {path.name}")
        unpack(path, src_dir)
        if index == 0:
            entries = list(src_dir.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                build_cwd = entries[0]

    env = build_env(recipe, ctx, pkg_dir, src_dir)
    log_path = work_dir / "build.log"
    print(f"==> Running build script (log: {log_path})")
    env["RIX_BUILD_SCRIPT"] = str(build_script)
    code = run_logged(["bash", "-e", "-c", BUILD_PRELUDE, "rix-build"],
                      build_cwd, env, log_path)
    if code != 0:
        raise RixError(f"Build of {full_name} failed with exit code {code}, see {log_path}")

    files = list_files(pkg_dir)
    if not files:
        raise RixError(
            f"Build of {full_name} installed nothing into $PKG "
            f"(forgot DESTDIR=\"$PKG\"?)"
        )

    outside = [f for f in files if not is_inside(PurePosixPath(f), ctx.allowed_dirs)]
    if outside:
        shown = "\n".join(f"  {f}" for f in outside[:10])
        more = f"\n  ... and {len(outside) - 10} more" if len(outside) > 10 else ""
        raise RixError(
            f"Build of {full_name} installed files outside {ctx.prefix} "
            f"(forgot --prefix=\"$PREFIX\"?):\n{shown}{more}"
        )

    print(f"==> Creating package ({len(files)} files)")
    archive = create_package(recipe, ctx, pkg_dir, files)
    print(f"==> Built {archive}")
    return archive


def ensure_deps(name, ctx):
    """Install everything `name` needs to be built, building it if needed.

    Like pkgsrc, rix builds against its own copies of libraries in the prefix,
    so build dependencies really get installed there. They are marked as not
    explicitly installed.
    """
    order = resolve([name], build_deps, ctx)
    for recipe in order[:-1]:  # the last one is `name` itself
        if find_installed(load_installed(ctx), recipe["name"]):
            continue
        archive = usable_archive(recipe, ctx) or build_package(recipe, ctx)
        install_package(archive, ctx, explicit=False)
    return order[-1]


def usable_archive(recipe, ctx):
    """An already built archive, if it matches the current USE flags."""
    archive = archive_path(recipe, ctx)
    if not archive.is_file():
        return None
    try:
        with tarfile.open(archive) as tar:
            info = read_pkginfo(tar, archive)
    except (RixError, tarfile.TarError):
        return None
    if sorted(info.get("use", [])) != enabled_use(recipe, ctx):
        print(f"==> USE flags of {recipe['name']} changed, it will be rebuilt")
        return None
    return archive


# ------------------------------------------------------- install / remove

def read_pkginfo(tar, archive):
    # .PKGINFO.json is written first, so usually only one member is read
    member = tar.next()
    if member is None or member.name != PKGINFO_NAME:
        try:
            member = tar.getmember(PKGINFO_NAME)
        except KeyError:
            raise RixError(f"{archive} is not a rix package (no {PKGINFO_NAME})")
    try:
        info = json.load(tar.extractfile(member))
    except json.JSONDecodeError as error:
        raise RixError(f"{archive}: broken {PKGINFO_NAME}: {error}")
    for field in ("name", "version", "files", "prefix"):
        if field not in info:
            raise RixError(f"{archive}: {PKGINFO_NAME} has no '{field}'")
    info.setdefault("depends", [])
    return info


def validate_members(members, archive, ctx):
    """Make sure the archive cannot write outside the places it may use.

    Installation keeps setuid bits and absolute symlinks (both are normal in a
    real system), so the safety checks are done here instead of by a tarfile
    filter.
    """
    allowed = ctx.allowed_dirs
    symlinks = set()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise RixError(f"{archive}: unsafe path in package: {member.name}")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise RixError(f"{archive}: unsupported file type: {member.name}")

        target = "/" / path
        # Directories may also be parents of the prefix (opt/, opt/rix/)
        is_parent = member.isdir() and any(target in d.parents for d in allowed)
        if not is_parent and not is_inside(target, allowed):
            raise RixError(f"{archive}: file outside {ctx.prefix}: {target}")

        if member.islnk():
            link = PurePosixPath(member.linkname)
            if link.is_absolute() or ".." in link.parts:
                raise RixError(f"{archive}: unsafe hard link: {member.name}")
        # A file must not be placed "through" a symlink from the same package
        for parent in path.parents:
            if str(parent) in symlinks:
                raise RixError(f"{archive}: path goes through a symlink: {member.name}")
        if member.issym():
            symlinks.add(member.name)


def same_content(tar, member, path):
    return path.is_file() and tar.extractfile(member).read() == path.read_bytes()


def install_package(archive, ctx, explicit=True, force=False):
    root = ctx.root
    etc_prefix = str(ctx.sysconfdir).lstrip("/") + "/"

    with tarfile.open(archive) as tar:
        info = read_pkginfo(tar, archive)
        name = info["name"]
        full_name = f"{name}-{info['version']}"

        # Paths are compiled into the binaries, so a package only works in
        # the prefix it was built for.
        if info["prefix"] != str(ctx.prefix):
            raise RixError(
                f"{full_name} was built for prefix {info['prefix']}, "
                f"not {ctx.prefix}; rebuild it with: rix build {name}"
            )
        check_writable(ctx)

        members = [m for m in tar.getmembers() if m.name != PKGINFO_NAME]
        validate_members(members, archive, ctx)

        db = load_installed(ctx)
        old = find_installed(db, name)

        if old and old["version"] == info["version"]:
            print(f"==> Reinstalling {full_name}")
        elif old:
            print(f"==> Upgrading {name} {old['version']} -> {info['version']}")
        else:
            print(f"==> Installing {full_name}")

        installed_names = {p["name"] for p in db["packages"]}
        for dep in info["depends"]:
            if dep not in installed_names:
                print(f"warning: {name} depends on {dep}, which is not installed",
                      file=sys.stderr)

        # Refuse to overwrite files that rix does not know about or that
        # belong to another package.
        owners = {f: p["name"] for p in db["packages"] for f in p["files"]}
        conflicts = []
        for path in info["files"]:
            if os.path.lexists(ctx.path(path)) and owners.get(path) != name:
                conflicts.append(f"  {path} ({owners.get(path, 'not owned by any package')})")
        if conflicts and not force:
            raise RixError(
                f"{full_name}: files already exist:\n" + "\n".join(conflicts)
                + "\nUse --force to overwrite them."
            )

        # Directories that this install creates; only these are removed later
        created_dirs = [
            "/" + m.name for m in members
            if m.isdir() and not os.path.lexists(ctx.path(m.name))
        ]

        for member in members:
            target = ctx.path(member.name)

            if member.isdir():
                # Keep existing directories (and dir symlinks like /lib) as they are
                if not os.path.lexists(target):
                    tar.extract(member, root, filter="fully_trusted")
                continue

            # On upgrade, don't clobber config files the user may have edited
            if (old and member.isfile() and member.name.startswith(etc_prefix)
                    and target.exists()):
                if not same_content(tar, member, target):
                    tar.extract(member.replace(name=member.name + ".rixnew"),
                                root, filter="fully_trusted")
                    print(f"warning: /{member.name} kept, new version saved as "
                          f"/{member.name}.rixnew", file=sys.stderr)
                continue

            # Extract next to the target and rename over it: replacing a file
            # this way is atomic and safe even for running programs and libc.
            tmp_name = member.name + ".rix-tmp"
            tar.extract(member.replace(name=tmp_name), root, filter="fully_trusted")
            os.replace(ctx.path(tmp_name), target)

    # Upgrade: drop files that the new version no longer ships
    dirs = created_dirs
    if old:
        new_files = set(info["files"])
        for path in old["files"]:
            if path not in new_files:
                remove_path(ctx, path)
        package_dirs = {"/" + m.name for m in members if m.isdir()}
        kept_dirs = [d for d in old.get("dirs", []) if d in package_dirs]
        remove_dirs(ctx, [d for d in old.get("dirs", []) if d not in package_dirs])
        dirs = sorted(set(kept_dirs) | set(created_dirs))
        # Installing a dependency by hand makes it explicit; never the reverse
        explicit = explicit or old.get("explicit", True)
        db["packages"].remove(old)

    db["packages"].append({
        "name": name,
        "version": info["version"],
        "depends": info["depends"],
        "explicit": explicit,
        "use": info.get("use", []),
        "files": info["files"],
        "dirs": dirs,
    })
    save_installed(ctx, db)
    run_ldconfig(ctx, info["files"])
    print(f"==> Installed {full_name}")


def run_ldconfig(ctx, files):
    """Refresh the linker cache when system libraries change.

    Only in system mode: a rix prefix uses rpath and never needs ldconfig.
    """
    if not ctx.system_mode or not any(".so" in f for f in files):
        return
    ldconfig = shutil.which("ldconfig") or "/sbin/ldconfig"
    command = [ldconfig] if ctx.root == Path("/") else [ldconfig, "-r", str(ctx.root)]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"warning: ldconfig failed: {error}", file=sys.stderr)


def remove_path(ctx, path):
    target = ctx.path(path)
    if target.is_symlink() or target.is_file():
        target.unlink()


def remove_dirs(ctx, dirs):
    # Deepest first; non-empty directories (still used by others) are kept
    for path in sorted(dirs, key=lambda d: d.count("/"), reverse=True):
        try:
            ctx.path(path).rmdir()
        except OSError:
            pass


def remove_package(name, ctx, force=False):
    db = load_installed(ctx)
    package = find_installed(db, name)
    if not package:
        raise RixError(f"Package is not installed: {name}")

    dependents = [p["name"] for p in db["packages"] if name in p.get("depends", [])]
    if dependents and not force:
        raise RixError(
            f"{name} is required by: {', '.join(dependents)}\n"
            f"Use --force to remove it anyway."
        )

    check_writable(ctx)
    print(f"==> Removing {name}-{package['version']}")
    # Files that another package also claims (after --force) stay in place
    others = {f for p in db["packages"] if p is not package for f in p["files"]}
    for path in package["files"]:
        if path not in others:
            remove_path(ctx, path)
    remove_dirs(ctx, package.get("dirs", []))

    db["packages"].remove(package)
    save_installed(ctx, db)
    run_ldconfig(ctx, package["files"])
    print(f"==> Removed {name}")


# ----------------------------------------------------------------- commands

def cmd_list(args):
    ctx = make_context(args)
    packages = load_installed(ctx)["packages"]

    if not packages:
        print(f"There are no packages installed in {ctx.prefix} yet.")
        return

    print(f"Installed packages in {ctx.prefix}:")
    for package in packages:
        mark = "" if package.get("explicit", True) else "  (dependency)"
        print(f"{package['name']} {package['version']}{mark}")


def cmd_search(args):
    ctx = make_context(args)
    installed = {p["name"] for p in load_installed(ctx)["packages"]}
    query = args.query.lower()

    found = False
    for recipe in load_repo():
        if query in recipe["name"].lower() or query in recipe["description"].lower():
            mark = " [installed]" if recipe["name"] in installed else ""
            print(f"{recipe['name']} {recipe['version']}{mark}")
            if recipe["description"]:
                print(f"    {recipe['description']}")
            found = True

    # Check the result only after all packages have been searched
    if not found:
        print(f"No packages found for: {args.query}")


def cmd_build(args):
    ctx = make_context(args)
    for name in args.packages:
        if args.no_deps:
            recipe = load_recipe(name)
        else:
            recipe = ensure_deps(name, ctx)
        build_package(recipe, ctx)


def cmd_install(args):
    ctx = make_context(args)

    # Paths to .tar.xz files are installed as they are; names are built
    # (with their dependencies) when needed.
    for arg in args.packages:
        if Path(arg).is_file():
            install_package(Path(arg), ctx, force=args.force)
            continue
        recipe = ensure_deps(arg, ctx)
        archive = usable_archive(recipe, ctx) or build_package(recipe, ctx)
        install_package(archive, ctx, force=args.force)


def cmd_remove(args):
    ctx = make_context(args)
    for name in args.packages:
        remove_package(name, ctx, force=args.force)


def cmd_autoremove(args):
    """Remove dependencies that nothing installed needs anymore."""
    ctx = make_context(args)
    removed = False
    # Loop: removing one orphan can orphan its own dependencies
    while True:
        db = load_installed(ctx)
        needed = {dep for p in db["packages"] for dep in p.get("depends", [])}
        orphans = [p["name"] for p in db["packages"]
                   if not p.get("explicit", True) and p["name"] not in needed]
        if not orphans:
            break
        for name in orphans:
            remove_package(name, ctx)
        removed = True
    if not removed:
        print("Nothing to remove.")


def cmd_upgrade(args):
    """Rebuild installed packages whose recipe version or USE flags changed."""
    ctx = make_context(args)
    db = load_installed(ctx)

    outdated = {}
    for package in db["packages"]:
        try:
            recipe = load_recipe(package["name"])
        except RixError:
            print(f"warning: no recipe for installed {package['name']}, skipping",
                  file=sys.stderr)
            continue
        # shortcut: any version difference counts as newer; add real version
        # comparison if downgrades in recipes ever become a thing.
        if recipe["version"] != package["version"]:
            outdated[recipe["name"]] = f"{package['version']} -> {recipe['version']}"
        elif sorted(package.get("use", [])) != enabled_use(recipe, ctx):
            outdated[recipe["name"]] = "USE changed"

    if not outdated:
        print("Everything is up to date.")
        return

    for name, reason in outdated.items():
        print(f"==> {name}: {reason}")

    # Dependencies first, so upgraded packages build against upgraded libraries
    for recipe in resolve(list(outdated), runtime_deps, ctx):
        if recipe["name"] not in outdated:
            continue
        ensure_deps(recipe["name"], ctx)
        archive = usable_archive(recipe, ctx) or build_package(recipe, ctx)
        # explicit=False keeps whatever the package was marked as before
        install_package(archive, ctx, explicit=False)


def cmd_info(args):
    ctx = make_context(args)
    db = load_installed(ctx)
    package = find_installed(db, args.package)
    try:
        recipe = load_recipe(args.package)
    except RixError:
        if not package:
            raise
        recipe = None

    print(f"Name:         {args.package}")
    if recipe:
        print(f"Description:  {recipe['description']}")
        print(f"Recipe:       {recipe['version']}")
        print(f"Depends:      {', '.join(runtime_deps(recipe, ctx)) or '-'}")
        print(f"Make depends: {', '.join(recipe['makedepends']) or '-'}")
        print(f"USE:          {format_use(recipe, ctx) or '-'}")

    if not package:
        print("Installed:    no")
        return
    required_by = [p["name"] for p in db["packages"] if args.package in p.get("depends", [])]
    reason = "explicitly" if package.get("explicit", True) else "as a dependency"
    print(f"Installed:    {package['version']} ({reason})")
    print(f"Built USE:    {' '.join(package.get('use', [])) or '-'}")
    print(f"Required by:  {', '.join(required_by) or '-'}")
    print(f"Files:        {len(package['files'])}")
    if args.files:
        for path in package["files"]:
            print(f"  {path}")


def cmd_owns(args):
    """Which installed package a file belongs to (pacman -Qo)."""
    ctx = make_context(args)
    # abspath, not resolve: the file itself may be a symlink owned by a package
    path = os.path.abspath(args.path)
    for package in load_installed(ctx)["packages"]:
        if path in package["files"]:
            print(f"{path} is owned by {package['name']} {package['version']}")
            return
    raise RixError(f"No package owns {path}")


def cmd_clean(args):
    """Delete build trees and cached files that current recipes don't use."""
    repo = load_repo()
    keep_sources = {s.rstrip("/").rsplit("/", 1)[-1]
                    for r in repo for s in r["source"] if "://" in s}
    keep_archives = {f"{r['name']}-{r['version']}.tar.xz" for r in repo}

    removed = 0
    if BUILD_DIR.is_dir():
        for path in BUILD_DIR.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
    for path in SOURCES_DIR.glob("*"):
        if path.name not in keep_sources:
            path.unlink()
            removed += 1
    for path in PACKAGES_DIR.glob("*/*"):
        if path.name not in keep_archives:
            path.unlink()
            removed += 1
    print(f"==> Removed {removed} item(s)")


def cmd_deps(args):
    ctx = make_context(args)
    installed = {p["name"]: p["version"] for p in load_installed(ctx)["packages"]}
    deps_of = runtime_deps if args.runtime else build_deps

    for recipe in resolve(args.packages, deps_of, ctx):
        marks = []
        if archive_path(recipe, ctx).is_file():
            marks.append("built")
        if recipe["name"] in installed:
            marks.append(f"installed {installed[recipe['name']]}")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        use_summary = format_use(recipe, ctx)
        use = f"  USE=\"{use_summary}\"" if use_summary else ""
        print(f"{recipe['name']} {recipe['version']}{use}{suffix}")


def cmd_use(args):
    """Show USE flags of packages, like Gentoo's `equery uses`."""
    ctx = make_context(args)
    db = load_installed(ctx)

    for name in args.packages:
        recipe = load_recipe(name)
        flags = effective_use(recipe, ctx)
        print(f"{name} {recipe['version']}")
        if not flags:
            print("    (no USE flags)")
            continue

        for flag, (enabled, source) in sorted(flags.items()):
            mark = "+" if enabled else "-"
            line = f"  {mark} {flag:<14} ({source})"
            description = recipe["use_description"].get(flag)
            if description:
                line += f"  {description}"
            extra = recipe["use_depends"].get(flag, []) + recipe["use_makedepends"].get(flag, [])
            if extra:
                line += f"  [needs: {', '.join(extra)}]"
            print(line)

        installed = find_installed(db, name)
        if installed and sorted(installed.get("use", [])) != enabled_use(recipe, ctx):
            was = " ".join(installed.get("use", [])) or "(none)"
            print(f"  note: installed with USE: {was}; apply with: rix install {name}")


def cmd_env(args):
    """Print shell commands that make the prefix usable: eval "$(rix env)"."""
    ctx = make_context(args)
    if ctx.system_mode:
        print(f"# prefix is {ctx.prefix}, nothing to set up")
        return
    p = ctx.prefix
    print(f'export PATH="{p}/bin:{p}/sbin:$PATH"')
    # A trailing ':' keeps the system's default man/info paths
    print(f'export MANPATH="{p}/share/man:$MANPATH"')
    print(f'export INFOPATH="{p}/share/info:$INFOPATH"')
    print(f'export PKG_CONFIG_PATH="{p}/lib/pkgconfig:{p}/share/pkgconfig:$PKG_CONFIG_PATH"')


def cmd_about(args):
    print(LOGO)
    print(f"Rix — a simple package manager {VERSION}")
    print("Install, remove, and search packages.")


def main():
    parser = argparse.ArgumentParser(
        description="Rix — a portable source-based package manager"
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"Rix {VERSION}",
    )

    parser.add_argument(
        "--prefix",
        help=f"Where packages live (default: $RIX_PREFIX, config, or {DEFAULT_PREFIX})",
    )

    parser.add_argument(
        "--root",
        help="Root of the target system (default: $RIX_ROOT or /)",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    list_parser = subparsers.add_parser(
        "list",
        help="Show installed packages",
    )

    about_parser = subparsers.add_parser(
        "about",
        help="Show information about Rix",
    )

    search_parser = subparsers.add_parser(
        "search",
        help="Search recipes by name or description",
    )

    search_parser.add_argument(
        "query",
        help="Text to search for",
    )

    build_parser = subparsers.add_parser(
        "build",
        help="Build packages (installing their dependencies into the prefix)",
    )

    build_parser.add_argument(
        "packages",
        nargs="+",
        help="Names of packages to build",
    )

    build_parser.add_argument(
        "--no-deps",
        action="store_true",
        help="Don't install missing dependencies first",
    )

    deps_parser = subparsers.add_parser(
        "deps",
        help="Show packages with all dependencies, in build order",
    )

    deps_parser.add_argument(
        "packages",
        nargs="+",
        help="Package names",
    )

    deps_parser.add_argument(
        "--runtime",
        action="store_true",
        help="Follow only runtime dependencies",
    )

    use_parser = subparsers.add_parser(
        "use",
        help="Show USE flags of packages and where their values come from",
    )

    use_parser.add_argument(
        "packages",
        nargs="+",
        help="Package names",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Install packages by name (building if needed) or .tar.xz path",
    )

    install_parser.add_argument(
        "packages",
        nargs="+",
        help="Package names or paths to .tar.xz files",
    )

    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite files that already exist",
    )

    remove_parser = subparsers.add_parser(
        "remove",
        help="Remove installed packages",
    )

    remove_parser.add_argument(
        "packages",
        nargs="+",
        help="Names of packages to remove",
    )

    remove_parser.add_argument(
        "--force",
        action="store_true",
        help="Remove even if other packages depend on it",
    )

    autoremove_parser = subparsers.add_parser(
        "autoremove",
        help="Remove dependencies that are no longer needed",
    )
    autoremove_parser.set_defaults(func=cmd_autoremove)

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Rebuild installed packages whose recipe or USE flags changed",
    )
    upgrade_parser.set_defaults(func=cmd_upgrade)

    info_parser = subparsers.add_parser("info", help="Show details about a package")
    info_parser.add_argument("package", help="Package name")
    info_parser.add_argument("--files", action="store_true", help="Also list installed files")
    info_parser.set_defaults(func=cmd_info)

    owns_parser = subparsers.add_parser("owns", help="Show which package owns a file")
    owns_parser.add_argument("path", help="Path to a file")
    owns_parser.set_defaults(func=cmd_owns)

    clean_parser = subparsers.add_parser(
        "clean",
        help="Delete build trees and sources/packages unused by current recipes",
    )
    clean_parser.set_defaults(func=cmd_clean)

    env_parser = subparsers.add_parser(
        "env",
        help='Print shell setup for the prefix: eval "$(rix env)"',
    )

    list_parser.set_defaults(func=cmd_list)
    about_parser.set_defaults(func=cmd_about)
    search_parser.set_defaults(func=cmd_search)
    build_parser.set_defaults(func=cmd_build)
    deps_parser.set_defaults(func=cmd_deps)
    use_parser.set_defaults(func=cmd_use)
    install_parser.set_defaults(func=cmd_install)
    remove_parser.set_defaults(func=cmd_remove)
    env_parser.set_defaults(func=cmd_env)

    args = parser.parse_args()
    try:
        args.func(args)
    except RixError as error:
        sys.stdout.flush()  # keep "==>" progress lines before the error
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as error:
        sys.stdout.flush()
        print(f"error: {error} (are you root?)", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
