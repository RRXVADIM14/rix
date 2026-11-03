# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Rix is a portable, source-based package manager and builder in the spirit of pkgsrc. It lives in its own prefix (default `/opt/rix`) next to any host system and builds its own copies of libraries there. From the host it uses only libc and the basic toolchain. Prefix `/usr` is "system mode": rix acts as the package manager of an LFS system, and `/etc` and `/var` are used instead of `$PREFIX/etc` and `$PREFIX/var`.

The code is Python, standard library only, and needs 3.11+ for `tomllib`. There is no build step, test suite, or linter. All logic lives in `main.py`.

## Running

```sh
./rix <command>          # wrapper; resolves main.py via readlink -f, can be symlinked into PATH
python3 main.py <command>
```

Commands: `list`, `search <text>`, `build [--no-deps] <pkg>...`, `deps [--runtime] <pkg>...`, `use <pkg>...`, `install [--force] <name|file.tar.xz>...`, `remove [--force] <pkg>...`, `autoremove`, `upgrade`, `info [--files] <pkg>`, `owns <path>`, `clean`, `env`, `about`, `--version`.

Global options go *before* the subcommand:
- `--prefix`: `$RIX_PREFIX`, then `prefix` in the config file, then `/opt/rix`.
- `--root`: `$RIX_ROOT`, then `/`. This is where the target system is mounted, for staging an image. When root is not `/`, the prefix directory is created automatically.

Config file: `$RIX_CONFIG`, or `$XDG_CONFIG_HOME/rix/config.toml` (default `~/.config/rix/config.toml`). See `config.example.toml`. It is validated in `check_settings`. The order of precedence is CLI > environment > config > default (`make_context`).

## Gentoo-style compile settings

- The config works like `make.conf` + `package.use` + `package.env`. Top-level keys are `prefix`, `cflags`, `cxxflags` (defaults to `cflags`), `ldflags`, `jobs`, and `use`. A `[package.<name>]` table can set `use`, `cflags`, `cxxflags`, `ldflags`, `jobs`, and `env` (extra variables for the build script).
- Per-package compile flags are resolved by `package_settings`, in this order: environment variable (a one-off `CFLAGS=-O0 rix build x`) > `[package.<name>]` > global config > default.
- **USE flags.** A recipe declares `[use]` as either `flag = true` or `flag = { default = false, description = "..." }`. `[use_depends]` and `[use_makedepends]` map a flag to extra deps, so `runtime_deps` and `build_deps` depend on `ctx`. `effective_use` resolves the flags: recipe default < global `use` < `[package.<name>] use`, where `"-flag"` disables. A global flag that a recipe doesn't have is ignored silently. An unknown flag in `[package.<name>]` warns once.
- The build script gets `USE="a b"` plus bash helpers from `BUILD_PRELUDE`: `use`, `use_enable`, `use_with`, `meson_use`, `meson_feature`, and `cmake_use`. The script is sourced via `bash -e -c PRELUDE`, with its path in `$RIX_BUILD_SCRIPT`. Write `if use x; then ...; fi`, not a bare `use x && ...` as the last line (it would fail the build).
- Enabled flags are stored in `.PKGINFO.json` and in the installed DB (`use`). `usable_archive` reuses a built archive only if its USE matches. Otherwise the package is rebuilt. `rix use <pkg>` shows each flag's value and source, and notes when the installed USE differs. Changing CFLAGS does *not* trigger a rebuild, same as in Gentoo.
- Dependencies that are no longer needed (e.g. after a flag is disabled) stay installed until `rix autoremove`. It repeatedly removes `explicit: false` packages that no installed package lists in `depends`, so build-only deps (`makedepends`) get removed too.

`/opt/rix` must be writable by the user (`sudo mkdir -p /opt/rix && sudo chown $USER: /opt/rix`). Rix itself never runs sudo. `check_writable` prints this hint.

**Testing:** don't touch `/opt/rix` or `/`. Set `RIX_PREFIX` to a scratch directory, or use `--root <scratch>`. Throwaway recipes such as `recipes/t*` can exercise the code path without downloading anything. Clean them up afterwards, along with `build/` and `packages/`.

## Recipes

The `zlib` and `hello` recipes are test/example recipes, not real packages. There are no real recipes yet.

`recipes/<name>/` contains:
- `package.toml`: `name` (must match the directory), `version`, optional `description`, `source` and `sha256` (parallel lists, same length), `depends` (runtime, and also needed for the build), and `makedepends` (build only). Dependencies are plain names, with no version constraints. A source containing `://` is downloaded. Anything else is a file in the recipe directory, such as a patch.
- `build`: a bash script run with `bash -e`, normally adapted from BLFS. It must use `--prefix="$PREFIX"` (and preferably `--sysconfdir="$SYSCONFDIR"`, `--localstatedir="$LOCALSTATEDIR"`) and install with `DESTDIR="$PKG"`. Inside the staging tree, paths are `"$PKG$PREFIX/..."`. Rix doesn't know about build systems.

Environment given to `build` (`build_env`):
- `PKG`, `SRC` (the directory with all unpacked sources), `NAME`, `VERSION`, `PREFIX`, `SYSCONFDIR`, `LOCALSTATEDIR`, `USE`.
- `CFLAGS`/`CXXFLAGS`/`LDFLAGS` from `package_settings` (default `-march=x86-64-v2`, never `native`), `MAKEFLAGS=-jN`, and the per-package `env`.
- Outside system mode, the prefix is prepended to `PATH`, `PKG_CONFIG_PATH`, `CMAKE_PREFIX_PATH`, `ACLOCAL_PATH`, and `CPPFLAGS`. `LDFLAGS` gets `-L` and `-Wl,-rpath,$PREFIX/lib`, so binaries find prefix libraries without `LD_LIBRARY_PATH`. With `--root`, the `-I`/`-L`/`PATH` entries point to the on-disk location, rpath keeps the real prefix, and `PKG_CONFIG_SYSROOT_DIR` is set.
- Host directories are still searched after the prefix. Nothing hides host libraries the way pkgsrc's buildlink does.

The script's working directory is the top-level directory of the first archive.

## Build pipeline

`build_package(recipe, ctx)`:
1. Wipe and recreate `build/<name>-<version>/{src,pkg}`.
2. Fetch the sources into the `sources/` cache. A cached file is reused if its checksum still matches. Downloads go to `.part` and are verified before being renamed.
3. Unpack (`filter="data"`) and run the script, with output also written to `build.log`.
4. Fail if `pkg/` is empty (missing `DESTDIR`) or contains files outside `ctx.allowed_dirs` (missing `--prefix`).
5. Write `packages/<prefix-slug>/<name>-<version>.tar.xz` (e.g. `packages/opt-rix/`). Paths are compiled into binaries, so archives are kept per prefix. `.PKGINFO.json` comes first and contains name, version, description, depends, **prefix**, built, and files. Entries are forced to `root:root`. The archive uses xz, not zstd, so it works on Python < 3.14.

## Dependencies

`resolve(names, deps_of)` does a depth-first topological sort and returns the recipes with dependencies first. It raises `RixError` on a cycle (showing the path) or a missing recipe. `deps_of` is either `build_deps` (`depends` + `makedepends`) or `runtime_deps`.

`ensure_deps(name, ctx)` is the pkgsrc-style core. Before `name` is built, every build dependency that isn't installed gets **installed into the prefix** (and built first if it has no archive), marked `explicit: false`. Rix builds against its own libraries, not the host's. Both `build` and `install <name>` go through `ensure_deps`. `build` always rebuilds the named packages. `install` reuses an existing archive. `install <file.tar.xz>` skips resolution and only warns about missing deps.

## Install / remove

- The database is `<root><localstatedir>/db/rix/installed.json`: `/opt/rix/var/db/rix/` normally, or `/var/db/rix/` in system mode. Each entry has `name`, `version`, `depends`, `explicit`, `files` (absolute target paths), and `dirs`. `dirs` lists only the directories this install *created*, so pre-existing directories are never removed. The database is written atomically (tmp + rename).
- Install refuses a package whose `prefix` differs from the current one. It then checks writability.
- Extraction uses `filter="fully_trusted"`, which keeps setuid bits and absolute symlinks. Safety comes from `validate_members` instead: it rejects absolute or `..` paths, files outside `allowed_dirs` (directories that are parents of the prefix are allowed), unsafe hard links, paths that go through a symlink in the same package, and special files.
- Each file is extracted to `<path>.rix-tmp` and moved into place with `os.replace`, so replacement is atomic. Existing directories are left untouched.
- Unowned or foreign existing files count as conflicts unless `--force` is given.
- On upgrade, files that were dropped are removed. An edited file under `SYSCONFDIR` is kept, and the new one is written as `.rixnew` (not tracked, survives `remove`).
- `remove` refuses when other packages depend on the package (`--force` overrides). It doesn't delete files that another package also claims.
- In system mode, install and remove run `ldconfig` (`-r <root>` for staging) when `.so` files change (`run_ldconfig`). If it fails, rix only prints a warning. A rix prefix relies on rpath instead.
- `upgrade` rebuilds and reinstalls installed packages whose recipe version differs (*any* difference counts; versions are not compared) or whose USE changed. Dependencies go first. Packages without a recipe are skipped.
- `clean` wipes `build/`. It also deletes files in `sources/` and `packages/*/` that no current recipe version references, including `.part` files.
- `env` prints `export` lines for `PATH`, `MANPATH`, `INFOPATH`, and `PKG_CONFIG_PATH` (`eval "$(rix env)"`).

## Conventions

- Expected failures raise `RixError`. `main()` prints them as `error: ...` and exits 1, with no traceback. `PermissionError` gets an "are you root?" hint.
- Tool-side paths (`recipes/`, `sources/`, `build/`, `packages/`) are resolved from `BASE_DIR` (the directory of `main.py`). Target-side paths always go through `Context` (`ctx.path()`, `ctx.prefix`, `ctx.db_file`).
- Bump `VERSION` in `main.py` when changing behavior.
- `rootfs/` is an unused placeholder, left from the postponed LFS-chroot plan.
