from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from automotive_log_mcp.core.redact import TOKEN_RE, Redactor
from automotive_log_mcp.core.templates import WILDCARD, mask

R = Redactor(b"k" * 64)


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("vin=WVWZZZ1JZXW000001 ok", "VIN", "WVWZZZ1JZXW000001"),
        ("VIN: 1FTFW1ET5DFC10312", "VIN", "1FTFW1ET5DFC10312"),
        ("imei 490154203237518 registered", "IMEI", "490154203237518"),
        ("peer=3c:5a:b4:01:02:03", "MAC", "3c:5a:b4:01:02:03"),
        ("bt AA-BB-CC-DD-EE-01 bonded", "MAC", "AA-BB-CC-DD-EE-01"),
        ("addr 192.168.1.23/24", "IP", "192.168.1.23"),
        ("Location[fused 48.137154,11.576124 hAcc=5]", "GPS", "48.137154,11.576124"),
        ("lat=48.1371 lon=11.5761", "GPS", "48.1371"),
        ("account max.mustermann@example.com added", "EMAIL", "max.mustermann@example.com"),
        ("[ro.serialno]: [EMULATOR35X1X11X0]", "SERIAL", "EMULATOR35X1X11X0"),
        ("androidboot.serialno=R58M12ABCDE", "SERIAL", "R58M12ABCDE"),
        ('connected ssid="HomeNet" rssi=-40', "SSID", "HomeNet"),
        ("call to +4915112345678 failed", "PHONE", "+4915112345678"),
    ],
)
def test_rules_catch(text: str, kind: str, value: str) -> None:
    out = R.redact(text)
    assert value not in out
    assert R.token(kind, value) in out
    assert (kind, value) in R.find(text)


@pytest.mark.parametrize(
    "text",
    [
        "Build fingerprint: 'google/sdk_car_x86_64/emulator_car64_x86_64:14/UAA1.250512.001/13398012:userdebug/test-keys'",
        "Start proc 1234:com.android.car.hvac/u0a42 for service com.android.car.hvac/.HvacService",
        "10-11 08:12:03.412 took 12ms, 0x7f12345678 #00 pc 000000000004e8a4 /system/lib64/libc.so",
        "version 14.0.1 sdk 34 build 13398012 uptime 1234567890",
        "loopback 127.0.0.1 any 0.0.0.0 mac 02:00:00:00:00:00",
        "ratio 0.5, 0.25 load average: 1.00, 0.90, 0.80",
        "uuid 123e4567-e89b-12d3-a456-426614174000 session 42",
        "android.hardware.automotive.vehicle@2.0-service",
        "FS/data/system/dropbox/data_app_crash@1767222001000.txt.gz",  # dropbox entry names
        "SYSTEM_TOMBSTONE@1767222001000.dat system_server_wtf@1767222001000.lost",
    ],
)
def test_no_false_positives(text: str) -> None:
    assert R.redact(text) == text
    assert R.find(text) == []


def test_tokens_are_keyed_and_stable() -> None:
    a = R.redact("vin=WVWZZZ1JZXW000001")
    assert a == R.redact("vin=WVWZZZ1JZXW000001")
    assert a != Redactor(b"other" * 13).redact("vin=WVWZZZ1JZXW000001")
    assert TOKEN_RE.search(a)


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=120))
def test_redaction_is_idempotent_and_leaves_nothing(text: str) -> None:
    once = R.redact(text)
    assert R.redact(once) == once
    assert R.find(once) == []


# -- templates ---------------------------------------------------------------------------------

WORDS = st.sampled_from(
    ["setProperty", "failed", "status=", "zone", "id:", "(", ")", "took", "ms,", "at", "x"]
)


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=120))
def test_mask_is_idempotent(text: str) -> None:
    assert mask(mask(text)) == mask(text)


@given(
    st.lists(st.one_of(WORDS, st.just("#")), min_size=1, max_size=12),
    st.data(),
)
def test_number_only_differences_share_a_template(parts: list[str], data: st.DataObject) -> None:
    def render() -> str:
        return " ".join(str(data.draw(st.integers(0, 10**9))) if p == "#" else p for p in parts)

    assert mask(render()) == mask(render())


def test_mask_examples() -> None:
    assert mask("setTemp 21.5 for zone 1") == "setTemp <*> for zone <*>"
    assert mask("prop=0x1f2e areaId=-3") == "prop=<*> areaId=<*>"
    assert mask("open /data/user/0/com.x/file.db failed") == "open <*> failed"
    assert mask("session 123e4567-e89b-12d3-a456-426614174000") == f"session {WILDCARD}"
    assert mask("peer=<MAC:abcdef12> rssi -40") == "peer=<MAC> rssi <*>"
    assert mask("HvacCtrl2 took 12ms") == "HvacCtrl2 took <*>ms"


def test_redaction_before_templates_keeps_raw_values_out() -> None:
    text = "connected to 3c:5a:b4:01:02:03 vin WVWZZZ1JZXW000001"
    tmpl = mask(R.redact(text))
    assert "3c:5a" not in tmpl and "WVWZZZ" not in tmpl
    assert re.search(r"<MAC>.*<VIN>", tmpl)
