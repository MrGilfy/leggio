# SPDX-License-Identifier: GPL-3.0-or-later
"""Tag editor"""

import logging

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, GObject, Pango

log = logging.getLogger(__name__)

class TagChip(Gtk.Box):
    def __init__(self, tag_name, on_remove=None, active=True, on_activate=None, **kwargs):
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            **kwargs
        )
        self.tag_name = tag_name
        self.active = active

        self.add_css_class("card")
        self.add_css_class("tag-chip")

        label = Gtk.Label(label=tag_name)
        label.set_margin_start(10)
        label.set_margin_top(2)
        label.set_margin_bottom(2)
        self.append(label)

        if active:
            self.add_css_class("tag-active")

            remove_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
            remove_btn.add_css_class("flat")
            remove_btn.add_css_class("circular")
            remove_btn.set_valign(Gtk.Align.CENTER)
            remove_btn.set_tooltip_text(f"Remove {tag_name}")

            if on_remove:
                remove_btn.connect("clicked", lambda _btn: on_remove(self.tag_name))

            self.append(remove_btn)
        else:
            label.set_margin_end(10)
            self.set_cursor_from_name("pointer")
            self.set_tooltip_text("Click to add")

            if on_activate:
                click_gesture = Gtk.GestureClick()
                click_gesture.connect(
                    "released",
                    lambda _gesture, _n_press, _x, _y: on_activate(self.tag_name)
                )
                self.add_controller(click_gesture)

class TagEditor(Adw.PreferencesRow):
    def __init__(self, current_tags=None, all_tags=None, num_default_suggestions=3, **kwargs):
        super().__init__(**kwargs)
        
        self.selected_tags = current_tags or []
        self.all_tags = all_tags or []
        self.num_default_suggestions = num_default_suggestions

        # Main container for the row
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # self.main_box.set_margin_top(12)
        # self.main_box.set_margin_bottom(12)
        # self.main_box.set_margin_start(12)
        # self.main_box.set_margin_end(12)
        self.set_child(self.main_box)

        # Entry for typing new tags
        # self.entry = Gtk.Entry(placeholder_text="Type a tag...")
        self.entry = Adw.EntryRow(title="Add tags...")
        self.entry.set_hexpand(True)
        self.entry.connect('changed', self.on_entry_changed)
        self.main_box.append(self.entry)

        self.suggestion_list = []

        self.tag_suggestion_flowbox = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=10,
            column_spacing=6,
            row_spacing=6
        )
        self.main_box.append(self.tag_suggestion_flowbox)

        # FlowBox to display selected tags
        self.tag_flowbox = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=10,
            column_spacing=6,
            row_spacing=6
        )
        self.main_box.append(self.tag_flowbox)

        # Key controller for Up/Down/Enter/Tab navigation
        key_controller = Gtk.EventControllerKey()
        key_controller.connect('key-pressed', self.on_key_pressed)
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.entry.add_controller(key_controller)

        # Initial render
        self.refresh_suggestions()
        self.refresh_tags()

    def get_tags(self):
        """Expose the list of currently selected tags."""
        return self.selected_tags

    def add_tag(self, tag):
        tag = tag.strip()
        if tag and tag not in self.selected_tags:
            self.selected_tags.append(tag)
            self.refresh_tags()
        self.entry.set_text("")
        self.refresh_suggestions()

    def remove_tag(self, tag):
        if tag in self.selected_tags:
            self.selected_tags.remove(tag)
            self.refresh_tags()

    def refresh_tags(self):
        """Redraws the tags in the FlowBox."""
        self.tag_flowbox.remove_all()
        for tag in self.selected_tags:
            chip = TagChip(tag_name=tag, on_remove=self.remove_tag)
            self.tag_flowbox.append(chip)
            
        self.tag_flowbox.set_visible(len(self.selected_tags) > 0)

    def refresh_suggestions(self):
        text = self.entry.get_text().lower().strip()

        if not text:
            self.suggestion_list.clear()
        else:
            self.suggestion_list = [t for t in self.all_tags if text in t.lower() and t not in self.selected_tags]

        if len(self.suggestion_list) == 0:
            self.suggestion_list = [t for t in self.all_tags if t not in self.selected_tags][:self.num_default_suggestions]

        self.tag_suggestion_flowbox.remove_all()
        for tag in self.suggestion_list:
            chip = TagChip(tag_name=tag, active=False, on_activate=self.add_tag)
            self.tag_suggestion_flowbox.append(chip)

        self.tag_suggestion_flowbox.set_visible(len(self.suggestion_list) > 0)

    def on_entry_changed(self, entry):
        self.refresh_suggestions()


    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            self.add_tag(self.entry.get_text())
            return True
        return False
