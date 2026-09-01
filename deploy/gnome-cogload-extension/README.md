# cogload GNOME Shell extension

Restores the `screen` channel (focused app + open-window count, hence
app-switch rate) under GNOME Wayland, where every other route is closed:

    org.gnome.Shell.Introspect.GetWindows            -> AccessDenied
    org.freedesktop.a11y.Manager KeyboardMonitor     -> AccessDenied
    ScreenCast portal                                -> refused (full-res frame)

## Install

    mkdir -p ~/.local/share/gnome-shell/extensions/cogload@orchestratormaxxing.local
    cp metadata.json extension.js \
       ~/.local/share/gnome-shell/extensions/cogload@orchestratormaxxing.local/
    # Wayland cannot restart the shell in place — log out and back in, then:
    gnome-extensions enable cogload@orchestratormaxxing.local

## Verify

    gdbus call --session --dest org.orchestratormaxxing.Cogload \
      --object-path /org/orchestratormaxxing/Cogload \
      --method org.orchestratormaxxing.Cogload.Sample
    # -> ('org.gnome.Nautilus.desktop', 7)

`cogload channels --redeclare` then flips `screen` back to true on its own;
without the extension it stays false and the collector degrades honestly.

## What it deliberately does not do

It never reads a window title. Titles carry document names, URLs and client
identities — the cogload design measures the *structure* of attention, never
its content. `tests/cogload/test_contract.py` greps this file to assert no
title-reading call appears in it.
