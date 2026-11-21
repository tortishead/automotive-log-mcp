# Walkthrough: follow a crash through the logs

One session on the test bugreport (`tests/fixtures/bugreport/`, zipped as by
`make_bugreport_zip` in `tests/test_golden.py`). Every call uses a different tool. The outputs
are real; long ones are cut where marked `…`.

Each call also works on the command line: `search(case='br', query='hvac', filter={'level': 'W'})`
is `automotive-log-mcp search br hvac --level W`.

## 1. Index the log: `add_source`

```
add_source(case='br', path='bugreport.zip')
```
```
Indexed bugreport.zip: 16 records in sources 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12.
  note: …tombstone_01.pb: unknown format, skipped
Next: list_sources(case) for formats and time ranges.
```

## 2. What is in the case: `list_sources`

```
list_sources(case='br')
```
```
1  logcat  2 records  2025-12-31 23:59:58.100 .. 2025-12-31 23:59:59.412  bugreport.zip!…#SYSTEM LOG:main
2  logcat  1 records  2026-01-01 00:00:02.000 .. 2026-01-01 00:00:02.000  bugreport.zip!…#SYSTEM LOG:system
3  logcat  1 records  2026-01-01 00:00:01.000 .. 2026-01-01 00:00:01.000  bugreport.zip!…#SYSTEM LOG:crash
4  logcat  2 records  …#EVENT LOG:events
5  kernel  3 records  0.000000 .. 124.000000  …#KERNEL LOG:kernel
…
fingerprint: google/sdk_car_x86_64/emulator_car64_x86_64:14/UAA1.250512.001/13398012:userdebug/test-keys
```

Each logcat buffer is its own source. The kernel log has only seconds since boot.

## 3. Where are the errors: `overview`

```
overview(case='br')
```
```
case 'br': 12 sources, 16 records, 12-31 23:50:00.000 .. 01-01 00:06:00.000 (16 min); since boot 0.000000 .. 124.000000
levels: F 4, E 4, W 2, I 6
top tags: HvacCtrl 2, java_crash 2, tombstone 2, ActivityManager 1, AndroidRuntime 1, …
top messages (templates, ×count, an example):
  ×1  E HvacCtrl  setProperty failed, status=NOT_AVAILABLE  e.g. log:1#2
  ×1  E AndroidRuntime  FATAL EXCEPTION: main  e.g. log:3#1
  ×1  E init  init: Service 'vendor.hvac' (pid <*>) exited with status <*>  e.g. log:5#2
  …
artifacts: anr 1, dropbox 1, java_crash 3, tombstone 2 (list_artifacts(case))
```

## 4. What crashed: `list_artifacts`

```
list_artifacts(case='br')
```
```
7 artifacts in 6 groups (same crash signature), by first time:
java_crash ×2 (1 duplicate)  java.lang.NullPointerException com.example.hvac: com.example.hvac.Controller.update < com.example.hvac.MainActivity.onFanClicked
  art:1  01-01 00:00:01.000  pid 1234  log:3#1  4 lines  Java crash in com.example.hvac (pid 1234, thread main): …
  art:4  01-01 00:00:01.000  pid 1234  log:9#1  23 lines  same crash as art:1 (data_app_crash@1767222001000.txt)
tombstone ×1  SIGSEGV android.hardware.automotive.vehicle@V1-example-service: libvhal_example.so!VehicleHal::setValue < …
  art:6  01-01 00:00:05.123  pid 812  log:11#1  50 lines  Native crash SIGSEGV in …
…
```

`art:4` is a copy of `art:1` from dropbox, so one read is enough.

## 5. Read the crash: `read_artifact`

```
read_artifact(case='br', id='art:1')
```
```
art:1  java_crash  record log:3#1  (4 lines)
time: 2026-01-01 00:00:01.000
process: com.example.hvac  pid 1234  tid 1250
signature: java.lang.NullPointerException com.example.hvac: com.example.hvac.Controller.update
same crash, other copies: art:4 (23 lines, data_app_crash@1767222001000.txt)
…
sections (cite lines as art:1/<section>:L<n>):
  art:1/header     L1-L2
  art:1/exception  L3-L4
next: read_artifact(id='art:1/exception')
```
```
read_artifact(case='br', id='art:1/exception')
```
```
L3  java.lang.NullPointerException: boom
L4  	at com.example.hvac.Controller.update(Controller.java:42)
```

## 6. What came just before: `context`

```
context(case='br', at='art:1', scope='pid', before=10, after=3)
```
```
pid 1234 around log:3#1, sources 1, 3, 9 by time:
  log:1#1  12-31 23:59:58.100  I 1234/1250 HvacCtrl  setTemp 21.5 for zone 1
  log:1#2  12-31 23:59:59.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE
> log:3#1  01-01 00:00:01.000  E 1234/1250 AndroidRuntime  FATAL EXCEPTION: main ⏎ … → art:1
  log:9#1  01-01 00:00:01.000  F 1234 java_crash  [art:4] Java crash in com.example.hvac (pid 1234): …
```

`scope='pid'` follows the process across the main and crash buffers.

## 7. Look in other sources: `search`

The kernel log has no wall clock, so a time filter leaves it out. Search it by source:

```
search(case='br', query='hvac', filter={'sources': ['*KERNEL*']})
```
```
1 match, 1 distinct message (collapsed):
log:5#2  123.456789  E init  init: Service 'vendor.hvac' (pid 1234) exited with status 1
code search (constant text of the templates):
  "init: Service 'vendor.hvac' (pid"  log:5#2
Outside the sources filter: 6 more matches (log:1 2, log:2 1, log:3 1, log:4 1, log:9 1); …
```

## 8. When did it happen: `count`

```
count(case='br', by='level', bucket='1m', filter={'from': 'art:1-2m', 'to': 'art:1+1m'})
```
```
7 records by level, 4 distinct values; bucket 1m (wall clock), 4 buckets:
  12-31 23:58:00.000  0
  12-31 23:59:00.000  2  ████████              E 1, I 1
  01-01 00:00:00.000  5  ████████████████████  F 2, E 1, I 1
  01-01 00:01:00.000  0
```

Times can be relative to a citation: `art:1-2m` is two minutes before the crash.

## 9. The full lines: `get_records`

```
get_records(case='br', ids=['log:1#2', 'log:5#2'])
```
```
log:1#2  2025-12-31 23:59:59.412  E  pid 1234 tid 1250  HvacCtrl
  as written: 12-31 23:59:59.412 · attrs: uid=1000 · code search: "setProperty failed, status=NOT_AVAILABLE"
  setProperty failed, status=NOT_AVAILABLE
log:5#2  123.456789  E  init
  code search: "init: Service 'vendor.hvac' (pid"
  init: Service 'vendor.hvac' (pid 1234) exited with status 1
```

## What the logs show

| Time | What | Cite |
| --- | --- | --- |
| 12-31 23:59:59.412 | `setProperty failed, status=NOT_AVAILABLE` in `com.example.hvac` | `log:1#2` |
| 01-01 00:00:01.000 | NullPointerException in `Controller.update` (`Controller.java:42`) | `art:1/exception:L4` |
| 01-01 00:00:02.000 | `com.example.hvac` (pid 1234) has died | `log:2#1` |
| 123.456789 s since boot | `vendor.hvac` (pid 1234) exited with status 1 | `log:5#2` |

The kernel time is not on the wall clock, so its place in this order is not shown by the logs.
