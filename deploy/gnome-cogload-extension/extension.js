// cogload window sampler — the ONLY way to measure app-switch rate under
// GNOME Wayland. Mutter exposes no client-side window list, and both
// org.gnome.Shell.Introspect.GetWindows and the GNOME 49+ accessibility API
// org.freedesktop.a11y.Manager.KeyboardMonitor return AccessDenied to a normal
// client (both verified on this box, 2026-08-19).
//
// THE PRIVACY INVARIANT IS THE WHOLE POINT. This returns an application ID —
// the Wayland equivalent of WM_CLASS, e.g. "org.gnome.Nautilus" — and a count.
// It MUST NEVER return window titles. Titles carry document names, URLs and
// client identities; that distinction is the entire privacy argument of the
// cogload design, and the contract greps this file to assert get_title() and
// friends never appear here. Do not "improve" this by adding a title.

import Shell from 'gi://Shell';
import Gio from 'gi://Gio';

const IFACE = `
<node>
  <interface name="org.orchestratormaxxing.Cogload">
    <method name="Sample">
      <arg type="s" direction="out" name="app_id"/>
      <arg type="u" direction="out" name="open_windows"/>
    </method>
  </interface>
</node>`;

class CogloadSampler {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/orchestratormaxxing/Cogload');
        this._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, 'org.orchestratormaxxing.Cogload',
            Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    disable() {
        if (this._owner) { Gio.bus_unown_name(this._owner); this._owner = null; }
        if (this._dbus) { this._dbus.unexport(); this._dbus = null; }
    }

    Sample() {
        const tracker = Shell.WindowTracker.get_default();
        const focused = global.display.focus_window;
        // get_app_for_window -> get_id() yields the .desktop id. This is the
        // application CLASS, not the document. No title is read anywhere.
        let appId = '';
        if (focused) {
            const app = tracker.get_window_app(focused);
            if (app)
                appId = app.get_id() || '';
        }
        const count = global.display.get_tab_list(0, null).length;
        return [appId, count];
    }
}

export default class CogloadExtension {
    enable()  { this._s = new CogloadSampler(); this._s.enable(); }
    disable() { if (this._s) { this._s.disable(); this._s = null; } }
}
