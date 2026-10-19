"""Global shortcut text <-> Win32 RegisterHotKey arguments (FR-14). Pure, no Qt.

Shortcuts are stored as text such as "Ctrl+Alt+Left". Each needs Ctrl, Alt or
Win so that ordinary typing in other programs is never captured.
"""

from __future__ import annotations

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

MODIFIERS = (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN))
KEYS: dict[str, int] = {
    **{c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
    **{f"F{n}": 0x6F + n for n in range(1, 25)},
    "Space": 0x20,
    "PgUp": 0x21,
    "PgDown": 0x22,
    "End": 0x23,
    "Home": 0x24,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "Ins": 0x2D,
    "Del": 0x2E,
}
_KEY_NAMES = {vk: name for name, vk in KEYS.items()}
_FOLD_KEYS = {name.casefold(): name for name in KEYS}
_FOLD_MODS = {name.casefold(): bit for name, bit in MODIFIERS}

# action -> label shown in the settings tab and in conflict messages
ACTIONS = {
    "toggle_visible": "顯示／隱藏歌詞",
    "toggle_lock": "鎖定／解鎖歌詞",
    "offset_earlier": "歌詞提前 0.1 秒",
    "offset_later": "歌詞延後 0.1 秒",
    "cue_line": "對齊開唱：下一句從現在開始",  # SPEC v1.29, versions of another length (an MV)
}
DEFAULTS = {
    "toggle_visible": "Ctrl+Alt+H",
    "toggle_lock": "Ctrl+Alt+L",
    "offset_earlier": "Ctrl+Alt+Left",
    "offset_later": "Ctrl+Alt+Right",
    "cue_line": "Ctrl+Alt+Down",
}
CALIBRATION = "Ctrl+Alt+M"  # calibration mode keeps its own key


def parse(text: str) -> tuple[int, int] | None:
    """"Ctrl+Alt+H" -> (modifiers, virtual key), or None when unusable."""
    *mods, key = [p.strip() for p in text.split("+")]
    modifiers = 0
    for name in mods:
        bit = _FOLD_MODS.get(name.casefold())
        if bit is None or modifiers & bit:
            return None
        modifiers |= bit
    if not modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        return None
    vk = KEYS.get(_FOLD_KEYS.get(key.casefold(), ""))
    return (modifiers, vk) if vk else None


def format_shortcut(modifiers: int, vk: int) -> str:
    parts = [name for name, bit in MODIFIERS if modifiers & bit]
    parts.append(_KEY_NAMES.get(vk, f"0x{vk:02X}"))
    return "+".join(parts)


def normalize(text: str) -> str | None:
    """Canonical spelling ("ctrl + alt + h" -> "Ctrl+Alt+H"); "" stays "" (no shortcut)."""
    if not text.strip():
        return ""
    parsed = parse(text)
    return format_shortcut(*parsed) if parsed else None


def problems(bindings: dict[str, str]) -> list[str]:
    """Messages for shortcuts that cannot all be registered together."""
    found = []
    seen: dict[str, str] = {}
    for action, text in bindings.items():
        if not text:
            continue
        label = ACTIONS.get(action, action)
        if text == CALIBRATION:
            found.append(f"{text} 保留給校對模式，「{label}」請改用其他組合")
        elif text in seen:
            found.append(f"「{seen[text]}」與「{label}」都設成 {text}")
        else:
            seen[text] = label
    return found
