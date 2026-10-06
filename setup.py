from cx_Freeze import Executable, setup

build_exe_options = {
    "packages": [
        "winrt",
        "winrt.windows.foundation",
        "winrt.windows.foundation.collections",
        "winrt.windows.media.control",
        "PySide6",
        "requests",
    ],
    "excludes": ["tkinter", "unittest", "pydoc"],
    "include_msvcr": True,
}

setup(
    name="LyricsOverlay",
    version="0.1",
    description="YT Music senkron söz overlay",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            "lyrics_overlay.py",
            base="gui",                  # konsol penceresi açılmasın
            target_name="LyricsOverlay.exe",
            # icon="simge.ico",          # isterseniz
        )
    ],
)