"""Shortcut tests run in a temporary folder; the real Startup folder is never touched."""

from pathlib import Path

from lyricfloat import autostart


def test_startup_dir_is_the_users_startup_folder():
    assert autostart.startup_dir().parts[-4:] == ("Windows", "Start Menu", "Programs", "Startup")


def test_launch_command_from_source_uses_windowed_python():
    target, args, _workdir = autostart.launch_command()
    assert args == "-m lyricfloat"
    assert Path(target).name.lower() in ("pythonw.exe", "python.exe")


def test_enable_creates_and_disable_removes_the_shortcut(tmp_path):
    folder = tmp_path / "Startup"
    assert not autostart.is_enabled(folder)
    autostart.set_enabled(True, folder)
    assert autostart.is_enabled(folder)
    assert (folder / "LyricFloat.lnk").stat().st_size > 0
    autostart.set_enabled(False, folder)
    assert not autostart.is_enabled(folder)


def test_disable_when_absent_is_harmless(tmp_path):
    autostart.set_enabled(False, tmp_path)
    assert not autostart.is_enabled(tmp_path)
