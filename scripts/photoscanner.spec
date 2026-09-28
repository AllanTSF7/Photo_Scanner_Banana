# PyInstaller spec for the packaged desktop build (one-folder; docs/HANDOFF.md #12).
# Build: .venv\Scripts\pyinstaller.exe scripts\photoscanner.spec --noconfirm --distpath dist --workpath build
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
ROOT = Path.cwd()

datas = [(str(ROOT / "banana" / "web" / "static"), "banana/web/static")]
binaries = []
hiddenimports = ["banana.web.api", "banana_core"]

# These three ship their own data (ONNX models, spaCy pipeline data, native extension DLLs) that plain
# import-analysis would miss.
for pkg in ("rapidocr_onnxruntime", "en_core_web_sm", "onnxruntime"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(Path(SPECPATH) / "desktop_launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PhotoScanner",
    console=False,
    icon=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    name="PhotoScanner",
)
