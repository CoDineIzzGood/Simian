# simian_launcher.spec (merge these bits into your current spec)
block_cipher = None

a = Analysis(
    ['simian_launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('data/generated/images', 'data/generated/images'),
        ('data/generated/video', 'data/generated/video'),
        ('data/generated/audio', 'data/generated/audio'),
        ('data/sandbox', 'data/sandbox'),
    ],
    hiddenimports=[
        'aiohttp',
        'PIL',
        'edge_tts',
        'playsound',
        'routes.generative',
        'generative.config',
        'generative.image_tools',
        'generative.video_tools',
        'generative.audio_tools',
        'generative.sandbox',
        'generative.safety',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Simian',
    debug=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='Simian'
)
