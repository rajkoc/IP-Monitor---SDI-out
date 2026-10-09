# PyInstaller runtime hook: nauči GStreamer / PyGObject gde su im fajlovi u paketu.
import os
import sys

base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
plugins = os.path.join(base, "lib", "gstreamer-1.0")

os.environ["GST_PLUGIN_PATH"] = plugins
os.environ["GST_PLUGIN_SYSTEM_PATH"] = plugins
os.environ["GST_PLUGIN_SCANNER"] = os.path.join(base, "gst-plugin-scanner.exe")
os.environ["GI_TYPELIB_PATH"] = os.path.join(base, "lib", "girepository-1.0")
os.environ["GST_REGISTRY"] = os.path.join(
    os.environ.get("TEMP", base), "ipmonitor-gst-registry.bin")

try:                       # Python 3.8+ na Windows-u: DLL-ovi pored programa
    os.add_dll_directory(base)
except (AttributeError, OSError):
    pass
