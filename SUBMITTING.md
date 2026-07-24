# Publishing to the AUR

The two packages live in `packaging/` in this repository, which is where
they are edited. The AUR itself is a separate git repository per package,
and only ever contains two files. Nothing in this repository is pushed to
the AUR directly — the files are copied across.

| This repository | AUR repository |
|---|---|
| `packaging/leggio/` | `ssh://aur@aur.archlinux.org/leggio.git` |
| `packaging/leggio-git/` | `ssh://aur@aur.archlinux.org/leggio-git.git` |

## One-time setup

**1. Create an AUR account** at <https://aur.archlinux.org/register>.

**2. Add an SSH key.** The AUR only accepts SSH, never HTTPS:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/aur -C "aur"
cat ~/.ssh/aur.pub
```

Paste the public key into *My Account → SSH Public Key* on the AUR site.

**3. Tell SSH to use that key for the AUR** — add to `~/.ssh/config`:

```
Host aur.archlinux.org
  User aur
  IdentityFile ~/.ssh/aur
  IdentitiesOnly yes
```

**4. Check it works:**

```sh
ssh aur@aur.archlinux.org help
```

You should get a list of commands, not a password prompt.

## Publishing a package for the first time

The AUR creates the repository on first push; there is nothing to create
on the website.

```sh
git clone ssh://aur@aur.archlinux.org/leggio.git aur-leggio
cd aur-leggio
cp ../leggio/packaging/leggio/PKGBUILD .
cp ../leggio/packaging/leggio/.SRCINFO .
git add PKGBUILD .SRCINFO
git commit -m "Initial import: leggio 0.1.0"
git push origin master
```

Repeat with `leggio-git`, whose repository is `leggio-git.git`.

> The AUR's default branch is **`master`**, not `main`.

## Updating an existing package

```sh
cd aur-leggio
cp ../leggio/packaging/leggio/PKGBUILD .
cp ../leggio/packaging/leggio/.SRCINFO .
git commit -am "Update to 0.2.0"
git push
```

`leggio-git` normally needs no update when a release is tagged: it builds
from the branch head and its `pkgver()` recalculates on every build. Push
it only when the PKGBUILD itself changes — a new dependency, say.

## What must be committed

**Only `PKGBUILD` and `.SRCINFO`.** The AUR rejects a push whose `.SRCINFO`
disagrees with its `PKGBUILD`, so regenerate it whenever the PKGBUILD
changes:

```sh
cd packaging/leggio
makepkg --printsrcinfo > .SRCINFO
```

Do **not** commit built packages (`*.pkg.tar.zst`), the `src/` or `pkg/`
directories makepkg leaves behind, or downloaded tarballs. A `.gitignore`
inside the AUR clone is a convenient guard:

```
*
!PKGBUILD
!.SRCINFO
```

## Before you push

```sh
cd packaging/leggio
makepkg --printsrcinfo > .SRCINFO   # keep it in step with the PKGBUILD
makepkg -f                          # must build cleanly
namcap PKGBUILD                     # packaging lint
namcap leggio-*.pkg.tar.zst         # built-package lint
```

`namcap` comes from the `namcap` package. If you have `devtools` installed,
`extra-x86_64-build` builds in a clean chroot, which catches dependencies
that only work because they happen to be installed on your machine — worth
doing before a first import.
