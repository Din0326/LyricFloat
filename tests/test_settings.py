import json

import pytest

from lyricfloat.settings import KNOWN_SOURCES, Settings, SettingsStore, SourceSetting, settings_from_dict


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "LyricFloat" / "settings.json")


def test_missing_file_gives_spec_defaults(store):
    s = store.load()
    assert s.font_px == 32
    assert s.text_color == "#FFFFFF"
    assert s.outline_px == 2.0 and s.outline_opacity == 0.7
    assert s.shadow_blur == 6.0 and s.shadow_offset_y == 2.0
    assert s.conversion_mode == "s2twp"
    assert s.alignment == "center"
    assert s.enabled_sources() == ["lrclib", "netease"]
    assert s.window_rect is None


def test_round_trip(store):
    s = Settings(font_px=40, alignment="left", conversion_mode="s2tw", window_rect=[10, 20, 800, 120], locked=True)
    s.sources = [SourceSetting("netease"), SourceSetting("lrclib", enabled=False)]
    store.save(s)
    loaded = store.load()
    assert loaded == s
    assert loaded.enabled_sources() == ["netease"]


def test_save_is_atomic_and_leaves_no_temp_file(store):
    store.save(Settings())
    assert store.path.exists()
    assert not store.path.with_suffix(".json.tmp").exists()
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema"] == 1


def test_corrupt_file_falls_back_to_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == Settings()


def test_non_object_json_falls_back_to_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == Settings()


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("conversion_mode", "s2t", "s2twp"),  # not an allowed mode
        ("alignment", "staggered", "center"),  # not in the MVP
        ("font_px", 4, 12),
        ("font_px", 500, 96),
        ("font_px", "big", 32),
        ("font_px", True, 32),
        ("locked", "yes", False),
        ("outline_opacity", -1, 0.7),
        ("text_color", "", "#FFFFFF"),
        ("window_rect", [1, 2, 3], None),
        ("window_rect", "0,0,1,1", None),
    ],
)
def test_bad_values_fall_back(key, value, expected):
    assert getattr(settings_from_dict({key: value}), key) == expected


def test_unknown_keys_are_ignored():
    assert settings_from_dict({"something_new": 1}) == Settings()


def test_sources_keep_order_drop_unknown_and_add_missing():
    s = settings_from_dict({"sources": [{"name": "netease", "enabled": False}, {"name": "bogus"}, "junk"]})
    assert [(x.name, x.enabled) for x in s.sources] == [("netease", False), ("lrclib", True)]
    assert {x.name for x in s.sources} == set(KNOWN_SOURCES)


def test_duplicate_sources_are_collapsed():
    s = settings_from_dict({"sources": [{"name": "lrclib"}, {"name": "lrclib", "enabled": False}]})
    assert [x.name for x in s.sources] == ["lrclib", "netease"]
    assert s.sources[0].enabled
