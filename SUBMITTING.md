# Publishing to the AUR

Leggio has two AUR packages. They are edited here in `packaging/`, and
then copied into the AUR, which is a separate git repository for each
package. Nothing is pushed to the AUR from this repository directly.

| Here | AUR |
|---|---|
| `packaging/leggio/` | `ssh://aur@aur.archlinux.org/leggio.git` |
| `packaging/leggio-git/` | `ssh://aur@aur.archlinux.org/leggio-git.git` |

## Setting up (once)

**1. Make an AUR account** at <https://aur.archlinux.org/register>.

**2. Make an SSH key.** The AUR only accepts SSH:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/aur -C "aur"
cat ~/.ssh/aur.pub
```

Paste the public key into *My Account → SSH Public Key* on the AUR site.

**3. Tell SSH to use that key.** Add this to `~/.ssh/config`:

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

You should see a list of commands, not a password prompt.

## Publishing a package the first time

The AUR creates the repository when you first push, so there is nothing
to set up on the website.

```sh
git clone ssh://aur@aur.archlinux.org/leggio.git aur-leggio
cd aur-leggio
cp ../leggio/packaging/leggio/PKGBUILD .
cp ../leggio/packaging/leggio/.SRCINFO .
git add PKGBUILD .SRCINFO
git commit -m "Initial import: leggio 0.1.0"
git push origin master
```

Do the same for `leggio-git`, using `leggio-git.git`.

> The AUR's main branch is called **`master`**, not `main`.

## Updating a package

```sh
cd aur-leggio
cp ../leggio/packaging/leggio/PKGBUILD .
cp ../leggio/packaging/leggio/.SRCINFO .
git commit -am "Update to 0.2.0"
git push
```

`leggio-git` usually doesn't need updating for a new release — it builds
from the latest code and works out its own version. Only push it when the
PKGBUILD itself changes, for example when a dependency is added.

## What goes in the AUR repository

**Only `PKGBUILD` and `.SRCINFO`.** The AUR rejects a push where the two
disagree, so regenerate `.SRCINFO` whenever you edit the PKGBUILD:

```sh
cd packaging/leggio
makepkg --printsrcinfo > .SRCINFO
```

Don't commit built packages (`*.pkg.tar.zst`), the `src/` and `pkg/`
folders makepkg leaves behind, or downloaded tarballs. A `.gitignore`
inside the AUR clone helps:

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
namcap PKGBUILD                     # checks the PKGBUILD
namcap leggio-*.pkg.tar.zst         # checks the built package
```

`namcap` is in the `namcap` package. If you have `devtools`,
`extra-x86_64-build` builds in a clean container, which catches missing
dependencies that only work because they happen to be installed on your
machine. Worth doing before a first import.
