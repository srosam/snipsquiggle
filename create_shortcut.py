"""Create SnipSquiggle Windows shortcuts.

Usage:
  python create_shortcut.py              Start Menu shortcut (one-shot snip)
  python create_shortcut.py --startup    Also run in tray mode at login
                                          (PrintScreen to snip, hidden window)
  python create_shortcut.py --uninstall  Remove both shortcuts
"""
import os
import sys
from win32com.client import Dispatch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "snipsquiggle.py")
ICON = os.path.join(HERE, "icon.ico")

# pythonw.exe = same folder as the current interpreter, no console window
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(PYW):
    PYW = sys.executable  # fall back to python.exe

PROGRAMS = os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs")
STARTUP = os.path.join(PROGRAMS, "Startup")

START_MENU_LNK = os.path.join(PROGRAMS, "SnipSquiggle.lnk")
STARTUP_LNK = os.path.join(STARTUP, "SnipSquiggle (tray).lnk")


def _make(lnk_path, arguments, description, window_style):
    shell = Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(lnk_path)
    sc.TargetPath = PYW
    sc.Arguments = arguments
    sc.WorkingDirectory = HERE
    sc.IconLocation = ICON
    sc.Description = description
    sc.WindowStyle = window_style
    sc.save()
    print("Created:", lnk_path)
    print("  ->", PYW, arguments)


def _remove(lnk_path):
    if os.path.exists(lnk_path):
        os.remove(lnk_path)
        print("Removed:", lnk_path)
    else:
        print("Not present:", lnk_path)


def install_start_menu():
    _make(START_MENU_LNK, f'"{SCRIPT}"',
          "Snip the screen and annotate with squiggly animated drawings",
          7)  # 7 = minimized (the launcher; the app draws its own overlay)


def install_startup():
    _make(STARTUP_LNK, f'"{SCRIPT}" --tray',
          "SnipSquiggle in the system tray — press PrintScreen to snip",
          7)  # pythonw is already windowless; keep the launcher out of the way
    print("\nSnipSquiggle will start in the tray at your next login.")
    print("Note: only one app can own PrintScreen — keep Greenshot etc. closed.")


def uninstall():
    _remove(START_MENU_LNK)
    _remove(STARTUP_LNK)


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    elif "--startup" in sys.argv:
        install_start_menu()
        install_startup()
    else:
        install_start_menu()
