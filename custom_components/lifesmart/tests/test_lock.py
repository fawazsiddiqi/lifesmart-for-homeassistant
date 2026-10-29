"""Tests for the LifeSmart lock state and unlock-AI parsing."""

from custom_components.lifesmart.lock import _is_locked, _unlock_ai


def test_is_locked():
    assert _is_locked({"type": 94, "val": 0})  # 实测 C200 关锁状态
    assert not _is_locked({"type": 95, "val": 0x2002})  # 指纹开锁
    assert _is_locked({"type": 95, "val": 0xF000})  # 方式 15 = 出错
    assert _is_locked({})


def test_unlock_ai():
    data = {"P1": {"valtag": "/ai/AzMAAPADjQEAABIcGwz__w/me/ai/AI1768483799"}}
    assert _unlock_ai(data) == "AI1768483799"
    assert _unlock_ai({"P1": {"valtag": ""}}) is None
    assert _unlock_ai({}) is None


def test_lock_event_sensors():
    from custom_components.lifesmart.binary_sensor import LOCK_EVENT_SENSORS

    triggered = {suffix: fn for _, suffix, _, _, fn in LOCK_EVENT_SENSORS}
    assert triggered["doorbell"]({"type": 1})
    assert not triggered["doorbell"]({"type": 0})
    assert triggered["duress"]({"val": 0b10})
    assert triggered["tamper"]({"val": 0b100})
    assert triggered["intrusion"]({"val": 1 << 8})
    assert not any(fn({"val": 1 << 4}) for s, fn in triggered.items() if s != "doorbell")  # 仅低电量


if __name__ == "__main__":
    test_is_locked()
    test_unlock_ai()
    test_lock_event_sensors()
    print("ok")
