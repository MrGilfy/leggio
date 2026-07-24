# Making a release

The version lives in exactly one place — `src/leggio/__init__.py`. Every
other file either reads it from there (`pyproject.toml`, the `Makefile`) or
is updated by this checklist (the metainfo, the PKGBUILD).

Two automated guards will fail the release if you skip a step: the tag must
match `__version__`, and the metainfo must describe the version being
tagged.

## Checklist

**1. Bump the version.**

```sh
$EDITOR src/leggio/__init__.py     # __version__ = "0.2.0"
make version                       # confirm it reads back
```

**2. Add the release to the metainfo.** A new `<release>` at the *top* of
`<releases>` in `data/io.github.MrGilfy.Leggio.metainfo.xml`:

```xml
<release version="0.2.0" date="2026-08-01">
  <description>
    <p>What changed, in a sentence.</p>
    <ul>
      <li>A user-visible change</li>
    </ul>
  </description>
</release>
```

Use the real date in `YYYY-MM-DD`. Release notes are read by software
centres, so write them for users, not as a commit log.

**3. Check locally.**

```sh
make check                         # desktop entry, AppStream, byte-compile
ruff check src tests
python tests/smoke.py              # needs a display
```

**4. Commit and tag.** The tag must be `v` plus the exact version.

```sh
git commit -am "Release 0.2.0"
git tag -a v0.2.0 -m "Leggio 0.2.0"
git push origin main --follow-tags
```

**5. Wait for the release workflow.** It validates the metadata, checks the
tag against `__version__`, builds `leggio-0.2.0.tar.gz`, creates the GitHub
release, and prints the tarball's sha256 in the job summary.

**6. Update the AUR package** with the version and checksum from that
summary:

```sh
cd packaging/leggio
$EDITOR PKGBUILD                   # pkgver=0.2.0, sha256sums=('...')
makepkg --printsrcinfo > .SRCINFO
makepkg -f                         # must build against the real tarball
```

Commit both files to this repository, then copy them to the AUR clone as
described in [SUBMITTING.md](SUBMITTING.md).

`leggio-git` needs nothing: it tracks the branch head and recomputes its
own `pkgver` at build time.

**7. Confirm.**

```sh
pacman -Sy leggio          # or: paru -S leggio
leggio --help 2>/dev/null || leggio
```

## If the release workflow fails

| Failure | Cause |
|---|---|
| `tag vX does not match __version__` | Step 1 was skipped, or the tag has a typo |
| `no <release version="X">` in the metainfo | Step 2 was skipped |
| AppStream validation failed | Malformed `<release>` block — run `make check` |

Delete the tag, fix, and re-tag:

```sh
git tag -d v0.2.0
git push origin :refs/tags/v0.2.0
```
