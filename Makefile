# SPDX-License-Identifier: GPL-3.0-or-later
#
# Install target shared by the PKGBUILDs, the Flatpak manifest and manual
# source installs.
#
#   make install                          -> /usr/local
#   sudo make install PREFIX=/usr         -> /usr
#   make install DESTDIR=/tmp/stage PREFIX=/usr    (packaging)
#
# Deliberately does not need python-build or python-installer: nothing here
# is compiled, so the modules are copied into $(PREFIX)/lib/leggio and put
# on sys.path by the launcher. That also keeps the install independent of
# the target's Python minor version, which a versioned site-packages path
# would not be. `pip install .` remains available for anyone who wants the
# site-packages route instead.

PREFIX  ?= /usr/local
DESTDIR ?=
PYTHON  ?= python3
INSTALL ?= install

APPID   := io.github.mrgilfy.leggio
# The ID before 0.4.0. Its files are removed on install, so an upgrade from
# source does not leave a second launcher behind.
OLDID   := io.github.MrGilfy.Leggio
VERSION := $(shell $(PYTHON) -c "import re, pathlib; \
	print(re.search(r'__version__ = \"([^\"]+)\"', \
	pathlib.Path('src/leggio/__init__.py').read_text()).group(1))")

bindir      := $(DESTDIR)$(PREFIX)/bin
libdir      := $(DESTDIR)$(PREFIX)/lib/leggio
sharedir    := $(DESTDIR)$(PREFIX)/share
appsdir     := $(sharedir)/applications
metainfodir := $(sharedir)/metainfo
iconsdir    := $(sharedir)/icons/hicolor
licensedir  := $(sharedir)/licenses/leggio

.PHONY: all version install uninstall check icons flatpak clean

all:
	@echo "Leggio $(VERSION)"
	@echo "  make install PREFIX=$(PREFIX)"
	@echo "  make check      validate desktop entry and AppStream metadata"
	@echo "  make icons      re-encode the viewer icons' PNG fallbacks"
	@echo "  make flatpak    build, install (--user) and bundle the Flatpak"

version:
	@echo $(VERSION)

install:
	# --- python package ---
	$(INSTALL) -d $(libdir)/leggio
	cp -r src/leggio/. $(libdir)/leggio/
	find $(libdir) -name '__pycache__' -type d -exec rm -rf {} +
	find $(libdir) -name '*.py[co]' -delete
	chmod -R a+rX $(libdir)
	# Byte-compile for the building interpreter; another Python just
	# recompiles in memory. -s/-p strip the staging prefix out of the
	# paths recorded inside the .pyc files.
	-$(PYTHON) -m compileall -q -o 0 -o 1 -s "$(DESTDIR)" -p / $(libdir)/leggio

	# --- launcher ---
	$(INSTALL) -d $(bindir)
	printf '%s\n' \
		'#!/usr/bin/env python3' \
		'# SPDX-License-Identifier: GPL-3.0-or-later' \
		'import sys' \
		'' \
		'sys.path.insert(0, "$(PREFIX)/lib/leggio")' \
		'' \
		'from leggio.cli import main' \
		'' \
		'sys.exit(main())' > $(bindir)/leggio
	chmod 755 $(bindir)/leggio

	# --- freedesktop metadata ---
	rm -f $(appsdir)/$(OLDID).desktop $(metainfodir)/$(OLDID).metainfo.xml \
		$(iconsdir)/scalable/apps/$(OLDID).svg \
		$(iconsdir)/128x128/apps/$(OLDID).png \
		$(iconsdir)/256x256/apps/$(OLDID).png
	$(INSTALL) -Dm644 data/$(APPID).desktop $(appsdir)/$(APPID).desktop
	$(INSTALL) -Dm644 data/$(APPID).metainfo.xml \
		$(metainfodir)/$(APPID).metainfo.xml
	$(INSTALL) -Dm644 data/icons/hicolor/scalable/apps/$(APPID).svg \
		$(iconsdir)/scalable/apps/$(APPID).svg
	$(INSTALL) -Dm644 data/icons/hicolor/128x128/apps/$(APPID).png \
		$(iconsdir)/128x128/apps/$(APPID).png
	$(INSTALL) -Dm644 data/icons/hicolor/256x256/apps/$(APPID).png \
		$(iconsdir)/256x256/apps/$(APPID).png
	$(INSTALL) -Dm644 LICENSE $(licensedir)/LICENSE

uninstall:
	rm -rf $(libdir)
	rm -f $(bindir)/leggio
	rm -f $(appsdir)/$(APPID).desktop $(appsdir)/$(OLDID).desktop
	rm -f $(metainfodir)/$(APPID).metainfo.xml $(metainfodir)/$(OLDID).metainfo.xml
	rm -f $(iconsdir)/scalable/apps/$(APPID).svg
	rm -f $(iconsdir)/128x128/apps/$(APPID).png
	rm -f $(iconsdir)/256x256/apps/$(APPID).png
	rm -rf $(licensedir)

check:
	desktop-file-validate data/$(APPID).desktop
	appstreamcli validate --no-net data/$(APPID).metainfo.xml
	$(PYTHON) -m compileall -q src/leggio
	@echo "checks passed"

# PNG fallbacks for the viewer icons, so they render without an SVG
# loader. Needs gtk4-encode-symbolic-svg (Ubuntu: libgtk-4-bin).
uiicons := src/leggio/ui/icons/hicolor

icons:
	for size in 24 32 48; do \
		$(INSTALL) -d $(uiicons)/$${size}x$${size}/actions; \
		for svg in $(uiicons)/scalable/actions/*.svg; do \
			gtk4-encode-symbolic-svg -o $(uiicons)/$${size}x$${size}/actions \
				"$$svg" $${size}x$${size} || exit 1; \
		done; \
	done

# Builds the same manifest CI does, installs it for the current user and
# writes a single-file bundle to build/. Needs flatpak-builder and
# org.gnome.Sdk; see docs/MAINTAINING.md.
manifest := packaging/flatpak/$(APPID).yml

flatpak:
	flatpak-builder --user --install --force-clean \
		--repo=build/flatpak-repo build/flatpak $(manifest)
	flatpak build-bundle build/flatpak-repo build/leggio-$(VERSION).flatpak \
		$(APPID) --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
	@echo "bundle: build/leggio-$(VERSION).flatpak"

clean:
	find src -name '__pycache__' -type d -exec rm -rf {} +
	rm -rf build dist src/*.egg-info
