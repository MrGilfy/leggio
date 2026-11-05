# Maintaining Leggio

## Checks

These are the checks CI runs. They need ruff, pytest, pycairo and the GTK
packages listed in the README.

```sh
ruff check src tests
python3 -m pytest
python3 tests/smoke.py    # needs a display; CI uses xvfb-run -a
make check                # desktop entry and AppStream metadata
```

## Making a release

The version lives only in `src/leggio/__init__.py`.

1. Set `__version__` and check it with `make version`.
2. Add a `<release version="X.Y.Z" date="YYYY-MM-DD">` block at the top of
   `<releases>` in `data/io.github.mrgilfy.leggio.metainfo.xml`. App stores
   show these notes, so write them for users, not as a list of commits.
3. Run the checks above.
4. Commit, tag and push. The tag is `v` plus the exact version:

   ```sh
   git commit -am "Release X.Y.Z"
   git tag -a vX.Y.Z -m "Leggio X.Y.Z"
   git push origin main --follow-tags
   ```

The release workflow then checks that the tag matches the source and the
metainfo, and builds two assets: `leggio-X.Y.Z.tar.gz` and
`leggio-X.Y.Z.flatpak`. It publishes the GitHub release with both and
with `sha256.txt` only after both builds succeed. The tarball's checksum
for the PKGBUILD is also printed in the job summary.

If the release fails:

| Error | Fix |
|---|---|
| `tag vX does not match __version__` | Step 1 was skipped, or the tag has a typo |
| `no <release version="X">` in the metainfo | Step 2 was skipped |
| AppStream validation failed | Run `make check` and fix the `<release>` block |

Delete the tag, fix the problem and tag again:

```sh
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z
```

## Flatpak

The manifest here is `packaging/flatpak/io.github.mrgilfy.leggio.yml`. It
builds the self-hosted bundle attached to each release. To build, install
and bundle it locally:

```sh
sudo dnf install flatpak-builder          # or your distribution's package
flatpak install flathub org.gnome.Sdk//51
make flatpak                              # writes build/leggio-X.Y.Z.flatpak
flatpak run io.github.mrgilfy.leggio
```

The GNOME runtime has no poppler-glib, so the manifest builds poppler and
poppler-data from pinned tarballs. To update one, change its `url` and set
`sha256` to the output of `sha256sum` on the new tarball. When moving to a
new GNOME runtime, also change the `gnome-NN` container image in
`.github/workflows/ci.yml` and `release.yml`.

## Flathub

Leggio is on Flathub as
[io.github.mrgilfy.leggio](https://flathub.org/apps/io.github.mrgilfy.leggio).
Its manifest lives in
[flathub/io.github.mrgilfy.leggio](https://github.com/flathub/io.github.mrgilfy.leggio),
not here, and MrGilfy maintains it by hand: Flathub does not accept
AI-written manifest changes. To ship a release, open a pull request there
that updates the source to the new tag. Flathub builds it on the PR, and
the update is published after the PR is merged.

## Updating the AUR packages

The two AUR packages are edited here and copied to the AUR, which has a
separate git repository for each:

| Here | AUR |
|---|---|
| `packaging/leggio/` | `ssh://aur@aur.archlinux.org/leggio.git` |
| `packaging/leggio-git/` | `ssh://aur@aur.archlinux.org/leggio-git.git` |

After a release, update `leggio`:

```sh
cd packaging/leggio
$EDITOR PKGBUILD                     # pkgver=X.Y.Z, pkgrel=1
updpkgsums                           # from pacman-contrib
makepkg --printsrcinfo > .SRCINFO
makepkg -f && namcap PKGBUILD leggio-*.pkg.tar.zst
```

Commit both files here, then copy them into your clone of the AUR
repository and push. The AUR branch is `master`, and it accepts only
`PKGBUILD` and `.SRCINFO`, which must agree:

```sh
cd ../aur-leggio
cp ../leggio/packaging/leggio/{PKGBUILD,.SRCINFO} .
git commit -am "Update to X.Y.Z"
git push
```

`leggio-git` works out its own version at build time. Push it only when
its PKGBUILD changes, for example when a dependency is added.

**First-time setup.** Register at <https://aur.archlinux.org/register>,
create a key with `ssh-keygen -t ed25519 -f ~/.ssh/aur` and paste
`~/.ssh/aur.pub` into *My Account*. Then add this to `~/.ssh/config`:

```
Host aur.archlinux.org
  User aur
  IdentityFile ~/.ssh/aur
```

`ssh aur@aur.archlinux.org help` should list commands. To publish a
package for the first time, clone its empty AUR repository with
`git clone ssh://aur@aur.archlinux.org/leggio.git aur-leggio`, copy the
two files in, commit and push.
