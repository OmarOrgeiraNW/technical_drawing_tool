# PyInstaller spec: pyinstaller packaging/DrawTool.spec  ->  dist/DrawTool/DrawTool(.exe)
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "packaging" / "drawtool_app.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("OCP"),
    datas=[(str(root / "drawtool" / "fonts"), "drawtool/fonts")],
    hiddenimports=collect_submodules("OCP"),
    excludes=["matplotlib", "PySide6", "PyQt5", "PyQt6", "IPython", "pandas", "scipy"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DrawTool", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="DrawTool")
