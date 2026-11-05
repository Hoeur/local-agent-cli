"""Optional full-screen terminal UI. Install with: pip install -e ".[tui]"

The core agent stays standard-library only; this package is imported only when Textual is
available and both stdin and stdout are terminals.
"""
import importlib.util


def available():
    return importlib.util.find_spec("textual") is not None


def probe_images():
    """Ask the terminal whether it can show Sixel or Kitty graphics, for the hologram's image mode.

    Terminals answer escape-sequence queries on stdin, so this must run before Textual takes the
    terminal over. It needs the optional textual-image package; any failure just means no images."""
    try:
        from textual_image import renderable, widget
    except ImportError:
        return {"protocol": None, "error": "install the hologram-image extra (textual-image and Pillow, Python 3.12+)"}
    except Exception as error:  # noqa: BLE001 - a failed probe must never stop the app
        return {"protocol": None, "error": f"terminal probe failed: {error}"}
    try:
        cell = widget.get_cell_size()
        cell_size = (max(1, cell.width), max(1, cell.height))
    except Exception:  # noqa: BLE001
        cell_size = (10, 20)
    chosen = renderable.Image
    if chosen is renderable.SixelImage:
        return {"protocol": "sixel", "widget": widget.SixelImage, "cell_size": cell_size, "error": None}
    if chosen is renderable.TGPImage:
        return {"protocol": "kitty", "widget": widget.TGPImage, "cell_size": cell_size, "error": None}
    return {"protocol": None, "error": "this terminal reported no Sixel or Kitty graphics support"}


def run(provider, state, data_dir, config, session, workspace=None, learn=False, auto_approve=False, **options):
    import os

    from .app import AgentApp
    from .theme import load_prefs

    prefs = load_prefs(data_dir)
    wants_images = "image" in {prefs.get("hologram_mode"), os.environ.get("AGENT_HOLOGRAM", "").lower()}
    app = AgentApp(provider, state, data_dir, config, session, workspace, learn, auto_approve,
                   prefs=prefs, image_support=probe_images() if wants_images else None, **options)
    app.run()
    return app.return_code or 0
