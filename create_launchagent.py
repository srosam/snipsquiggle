"""Run SnipSquiggle in the macOS menu bar at login (LaunchAgent).

Usage:
  python3 create_launchagent.py             install (RunAtLoad, --tray)
  python3 create_launchagent.py --uninstall remove it

Notes:
  * Run this with the SAME python you use for the app (a Tk 8.6 build — see the
    README's macOS setup). Its path gets baked into the plist.
  * A bare script (not a .app bundle) may show a Dock icon as well as the
    menu-bar icon. That's cosmetic; the hotkey and menu still work.
"""
import os
import sys
import subprocess

LABEL = "com.snipsquiggle.tray"
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "snipsquiggle.py")
PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")
PLIST = os.path.join(PLIST_DIR, LABEL + ".plist")

PLIST_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{SCRIPT}</string>
        <string>--tray</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>ProcessType</key><string>Interactive</string>
    <key>WorkingDirectory</key><string>{HERE}</string>
</dict>
</plist>
"""


def install():
    os.makedirs(PLIST_DIR, exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(PLIST_XML)
    subprocess.run(["launchctl", "unload", PLIST],
                   check=False, capture_output=True)
    subprocess.run(["launchctl", "load", PLIST], check=False)
    print("Installed LaunchAgent:", PLIST)
    print("  ->", sys.executable, SCRIPT, "--tray")
    print("\nSnipSquiggle will start in the menu bar at login (and now).")
    print("Only one app can own the hotkey — keep other grabbers closed.")


def uninstall():
    subprocess.run(["launchctl", "unload", PLIST], check=False)
    if os.path.exists(PLIST):
        os.remove(PLIST)
        print("Removed:", PLIST)
    else:
        print("Not present:", PLIST)


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("create_launchagent.py is macOS-only. "
                 "On Windows use create_shortcut.py --startup.")
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()
