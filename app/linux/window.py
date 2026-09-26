"""Small native window for the OS3 Router app on Linux (GTK 3 + WebKitGTK), used when no Chromium-family
browser is installed. Exits 1 when WebKitGTK isn't available, so the launcher falls back to the browser."""
import os, sys

try:
    import gi
    gi.require_version("Gtk", "3.0")
    for v in ("4.1", "4.0"):
        try:
            gi.require_version("WebKit2", v)
            break
        except ValueError:
            continue
    from gi.repository import Gtk, WebKit2
except (ImportError, ValueError):
    sys.exit(1)

url = sys.argv[1]
win = Gtk.Window(title="OS3 Router")
win.set_default_size(1100, 760)
win.set_wmclass("os3-router", "OS3 Router")
icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "codex_os3", "ui", "guide", "app-icon.png")
if os.path.exists(icon):
    win.set_icon_from_file(icon)
view = WebKit2.WebView()


def policy(v, decision, kind):
    # only the router's own page loads here; other links open in the normal browser
    if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION or kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
        uri = decision.get_navigation_action().get_request().get_uri()
        if not uri.startswith(url.split("/app")[0]):
            Gtk.show_uri_on_window(win, uri, Gtk.get_current_event_time())
            decision.ignore()
            return True
    return False


view.connect("decide-policy", policy)
view.load_uri(url)
win.add(view)
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
