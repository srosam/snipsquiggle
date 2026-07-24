"""Create a SnipSquiggle shortcut in the Start Menu (windowless launch)."""
import os
import sys
from win32com.client import Dispatch

HERE = os.path.dirname(os.path.abspath(__file__))
script = os.path.join(HERE, "snipsquiggle.py")
icon = os.path.join(HERE, "icon.ico")

# pythonw.exe = same folder as the current interpreter, no console window
pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(pyw):
    pyw = sys.executable  # fall back to python.exe

start_menu = os.path.join(os.environ["APPDATA"],
                          r"Microsoft\Windows\Start Menu\Programs")
lnk_path = os.path.join(start_menu, "SnipSquiggle.lnk")

shell = Dispatch("WScript.Shell")
sc = shell.CreateShortCut(lnk_path)
sc.TargetPath = pyw
sc.Arguments = f'"{script}"'
sc.WorkingDirectory = HERE
sc.IconLocation = icon
sc.Description = "Snip the screen and annotate with squiggly animated drawings"
sc.WindowStyle = 7  # minimized (the launcher window; app draws its own overlay)
sc.save()

print("Created shortcut:")
print(" ", lnk_path)
print("  ->", pyw, sc.Arguments)
