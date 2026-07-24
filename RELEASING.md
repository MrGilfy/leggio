# Making a release

The version number is written in one file only: `src/leggio/__init__.py`.
Everything else either reads it from there or is updated by the steps
below.

## Steps

**1. Change the version.**

```sh
$EDITOR src/leggio/__init__.py     # __version__ = "0.2.0"
make version                       # check it reads back correctly
```

**2. Write the release notes.** Add a new `<release>` block at the top of
`<releases>` in `data/io.github.MrGilfy.Leggio.metainfo.xml`:

```xml
<release version="0.2.0" date="2026-08-01">
  <description>
    <p>What changed, in a sentence.</p>
    <ul>
      <li>Something users will notice</li>
    </ul>
  </description>
</release>
```

Use a real date in `YYYY-MM-DD` form. App stores show these notes to
users, so write them for users and not as a list of commits.

**3. Test it.**

```sh
make check                         # checks the desktop and AppStream files
ruff check src tests
python tests/smoke.py              # needs a display
```

**4. Commit and tag.** The tag is `v` plus the exact version.

```sh
git commit -am "Release 0.2.0"
git tag -a v0.2.0 -m "Leggio 0.2.0"
git push origin main --follow-tags
```

**5. Wait for GitHub Actions.** It checks the tag matches the version,
builds `leggio-0.2.0.tar.gz`, creates the GitHub release, and prints the
tarball's sha256 in the job summary.

**6. Update the AUR package.**

```sh
cd packaging/leggio
$EDITOR PKGBUILD                   # pkgver=0.2.0
updpkgsums                         # downloads the release and fills in the checksum
makepkg --printsrcinfo > .SRCINFO
makepkg -f                         # must build
```

`updpkgsums` comes from `pacman-contrib`. Commit both files here, then
copy them to the AUR as described in [SUBMITTING.md](SUBMITTING.md).

`leggio-git` needs nothing — it follows the branch and works out its own
version at build time.

**7. Check it worked.**

```sh
paru -S leggio
leggio
```

## If the release fails

| Error | Reason |
|---|---|
| `tag vX does not match __version__` | Step 1 was skipped, or the tag has a typo |
| `no <release version="X">` in the metainfo | Step 2 was skipped |
| AppStream validation failed | The `<release>` block is malformed — run `make check` |

Delete the tag, fix the problem, and tag again:

```sh
git tag -d v0.2.0
git push origin :refs/tags/v0.2.0
```
