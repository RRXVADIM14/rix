```
████  ███ █   █
█   █  █   █ █
████   █    █
█  █   █   █ █
█   █ ███ █   █
```

**English** · [Русский](README.ru.md)

# rix

A portable, source-based package manager and builder, in the spirit of [pkgsrc](https://www.pkgsrc.org/), with Gentoo-style USE flags.

rix lives in its own prefix (`/opt/rix` by default), next to whatever system you already run. It builds its own copies of libraries there and takes only libc and a compiler from the host. Recipes are a few lines of shell, usually copied straight from the [BLFS book](https://www.linuxfromscratch.org/blfs/).

With prefix `/usr`, rix becomes the system package manager of a Linux From Scratch system. One tool covers both cases.

> **Status:** early and experimental. The core works and is tested on throwaway recipes, but there are only two real recipes so far (`zlib`, `hello`).

## Features

- **Builds from source**: sources are downloaded, sha256-verified, cached, and built into binary packages (`.tar.xz`)
- **Its own prefix**: binaries get an rpath into the prefix and run without `LD_LIBRARY_PATH`
- **Dependencies**: runtime and build-only, resolved in the right order. Missing dependencies are built and installed automatically. Cycles are detected.
- **USE flags and per-package compile settings**, Gentoo-style (`make.conf` + `package.use` + `package.env`)
- **Safe install/remove**: file conflicts are detected, files are replaced atomically, your edited configs are kept (`.rixnew`), and directories rix didn't create are never deleted
- **Upgrade, autoremove, ownership queries, cache cleanup**
- **Pure Python standard library**: no dependencies to install

## Requirements

- Linux
- Python 3.11+
- bash, a C compiler, and whatever the recipes need (make, meson, cmake, …)

## Quick start

```sh
git clone https://github.com/RRXVADIM14/rix.git
cd rix

# One-time setup: create the prefix and give it to your user (the only sudo step)
sudo mkdir -p /opt/rix && sudo chown $USER: /opt/rix

./rix install hello            # build and install into /opt/rix
eval "$(./rix env)"            # add /opt/rix/bin etc. to your shell (put this in ~/.bashrc)
hello
```

The `rix` script can be symlinked into your `PATH`: `ln -s "$PWD/rix" ~/.local/bin/rix`.

## Commands

| Command | Description |
|---|---|
| `rix search <text>` | Search recipes by name or description |
| `rix build <pkg>…` | Build packages (installing missing dependencies into the prefix first) |
| `rix install <pkg\|file.tar.xz>…` | Install by name (building if needed) or from a package file |
| `rix remove <pkg>…` | Remove packages (refuses if something depends on them) |
| `rix upgrade` | Rebuild installed packages whose recipe version or USE flags changed |
| `rix autoremove` | Remove dependencies nothing needs anymore |
| `rix list` | Show installed packages |
| `rix info <pkg> [--files]` | Package details, optionally with its file list |
| `rix owns <path>` | Which package a file belongs to |
| `rix deps <pkg>…` | Dependencies in build order, with their status |
| `rix use <pkg>…` | USE flags and where their values come from |
| `rix clean` | Delete build trees and cached files unused by current recipes |
| `rix env` | Print shell setup for the prefix |

Global options go before the command: `--prefix DIR` (or `$RIX_PREFIX`) and `--root DIR` (or `$RIX_ROOT`, for preparing another system, such as an image).

## Writing a recipe

A recipe is a directory `recipes/<name>/` with two files.

`package.toml`:

```toml
name = "zlib"
version = "1.3.1"
description = "Compression library implementing the deflate method"
source = ["https://zlib.net/fossils/zlib-1.3.1.tar.gz"]
sha256 = ["9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23"]
depends = []          # needed at runtime (and to build)
makedepends = []      # needed only to build

[use]
static = { default = false, description = "Also install the static library libz.a" }

[use_depends]         # extra dependencies when a flag is on
# ssl = ["openssl"]
```

`build` is a bash script run inside the unpacked source tree:

```sh
./configure --prefix="$PREFIX"
make
make DESTDIR="$PKG" install

if ! use static; then
    rm -f "$PKG$PREFIX/lib/libz.a"
fi
```

The rules:
- configure with `--prefix="$PREFIX"`, and preferably `--sysconfdir="$SYSCONFDIR"` too
- install with `DESTDIR="$PKG"`

rix doesn't care which build system the script uses: autotools, meson and cmake all work. The script gets `PKG`, `SRC`, `NAME`, `VERSION`, `PREFIX`, `SYSCONFDIR`, `LOCALSTATEDIR`, `USE`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS` and `MAKEFLAGS`, plus these helpers: `use`, `use_enable`, `use_with`, `meson_use`, `meson_feature`, `cmake_use`.

A source without `://` is a file next to the recipe, such as a patch: `patch -Np1 -i "$SRC/fix.patch"`.

## Configuration

`~/.config/rix/config.toml` is optional. See [`config.example.toml`](config.example.toml).

```toml
prefix = "/opt/rix"
cflags = "-O2 -pipe -march=x86-64-v2"
jobs = 8
use = ["-doc"]                 # global USE flags; "-flag" disables

[package.zlib]                 # per-package settings
use = ["static"]
cflags = "-O3 -pipe -march=x86-64-v2"
```

Precedence for USE flags: recipe default < global `use` < `[package.<name>]`.
Precedence for compile flags: environment (`CFLAGS=-O0 rix build x`) > `[package.<name>]` > global > default.

Changing a package's USE flags makes rix rebuild it on the next `install` or `upgrade`.

## System mode (LFS)

```sh
rix --prefix /usr install zlib
```

With prefix `/usr`, configs go to `/etc`, the database to `/var/db/rix`, and `ldconfig` runs after libraries change. This is how rix manages a Linux From Scratch system.

## Limitations

- Dependencies are plain names, without version constraints
- Host libraries are still visible during builds. A dependency missing from a recipe is silently taken from the host (pkgsrc's buildlink hides them; rix doesn't yet).
- Package files only work in the prefix they were built for
- No signatures on packages and no remote binary repository
