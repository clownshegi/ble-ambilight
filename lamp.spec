from PyInstaller.utils.hooks import collect_all

datas = [('profile.json', '.')]
binaries = []
hiddenimports = []

for pkg in ('bleak', 'dxcam', 'aiohttp', 'pyaudiowpatch', 'mss'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

block_cipher = None

a_ambilight = Analysis(
    ['ambilight.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz_ambilight = PYZ(a_ambilight.pure, cipher=block_cipher)
exe_ambilight = EXE(
    pyz_ambilight,
    a_ambilight.scripts,
    [],
    exclude_binaries=True,
    name='Ambilight',
    console=True,
    icon='lamp.ico',
)

a_lamp = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz_lamp = PYZ(a_lamp.pure, cipher=block_cipher)
exe_lamp = EXE(
    pyz_lamp,
    a_lamp.scripts,
    [],
    exclude_binaries=True,
    name='Lamp',
    console=False,
    icon='lamp.ico',
)

coll = COLLECT(
    exe_ambilight,
    a_ambilight.binaries,
    a_ambilight.zipfiles,
    a_ambilight.datas,
    exe_lamp,
    a_lamp.binaries,
    a_lamp.zipfiles,
    a_lamp.datas,
    strip=False,
    upx=False,
    name='LampSuite',
)

