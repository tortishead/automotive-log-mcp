"""One synthetic fixture per format variant, compared with golden records (tests/golden/).

Regenerate after an intended change with ``UPDATE_GOLDEN=1 pytest tests/test_golden.py`` and
review the diff.
"""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

import pytest

from automotive_log_mcp.app import App

from .conftest import FIXTURES, check_golden, copy_fixture, dump_case

TEXT_FIXTURES = sorted(
    str(p.relative_to(FIXTURES)).replace("\\", "/")
    for p in FIXTURES.rglob("*")
    if p.is_file() and p.parent.name != "bugreport"
)


@pytest.mark.parametrize("rel", TEXT_FIXTURES)
def test_fixture_golden(app: App, tmp_path: Path, rel: str) -> None:
    path = copy_fixture(rel, tmp_path)
    result = app.ingestor.add(app.config.case_db("g"), str(path))
    assert result.files[0].records > 0
    check_golden(rel.replace("/", "__"), dump_case(app, "g"))


PROTO_TOMBSTONE = b"\x0a\x1b\x08\x84\x07\x10\x93\x07\x1a\x0bAudioStream\x00\x01\x02"  # not parsed (M5)
WTF = """\
Process: system_server
Subsystem: CarService
Build: google/sdk_car_x86_64/emulator_car64_x86_64:14/UAA1.250512.001/13398012:userdebug/test-keys

android.util.Log$TerribleFailure: Unexpected power state transition 3 -> 7
\tat android.util.Log.wtf(Log.java:338)
\tat com.android.car.power.CarPowerManagementService.onApPowerStateChange(CarPowerManagementService.java:812)
\tat com.android.car.power.CarPowerManagementService$PowerHandler.handleMessage(CarPowerManagementService.java:1990)
"""


def make_bugreport_zip(tmp_path: Path) -> Path:
    src = next((FIXTURES / "bugreport").glob("bugreport-*.txt"))
    zpath = tmp_path / (src.stem + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("version.txt", (2026, 1, 1, 0, 10, 0)), "2.0")
        zf.writestr(zipfile.ZipInfo("main_entry.txt", (2026, 1, 1, 0, 10, 0)), src.name)
        zf.write(src, src.name)
        when = (2026, 1, 1, 0, 10, 0)
        for name in ("tombstone_00", "tombstone_01"):
            zf.write(FIXTURES / "tombstone" / name, f"FS/data/tombstones/{name}")
        zf.writestr(zipfile.ZipInfo("FS/data/tombstones/tombstone_01.pb", when), PROTO_TOMBSTONE)
        # Dropbox: the crash of com.example.hvac (also in the crash buffer), compressed as on a
        # device, and a wtf from system_server. Times in the names are UTC (device: Europe/Berlin).
        crash = (FIXTURES / "dropbox" / "data_app_crash@1767222001000.txt").read_bytes()
        zf.writestr(
            zipfile.ZipInfo("FS/data/system/dropbox/data_app_crash@1767222001000.txt.gz", when),
            gzip.compress(crash, mtime=0),
        )
        zf.writestr(zipfile.ZipInfo("FS/data/system/dropbox/system_server_wtf@1767222200000.txt", when), WTF)
        zf.writestr(
            zipfile.ZipInfo("FS/data/misc/logd/logcat.01", (2026, 1, 1, 0, 10, 0)),
            "12-31 23:50:00.000  1000  1100 I PersistLog: older line\n",
        )
    return zpath


def test_bugreport_zip_golden(app: App, tmp_path: Path) -> None:
    zpath = make_bugreport_zip(tmp_path)
    result = app.ingestor.add(app.config.case_db("br"), str(zpath))
    f = result.files[0]
    assert any("tombstone_01.pb" in n and "unknown format" in n for n in f.notes)
    check_golden("bugreport_zip", dump_case(app, "br"))
