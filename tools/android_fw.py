#!/usr/bin/env python3
"""Query firmware details from connected Android devices over adb, and search them.

Read-only: it only runs `getprop`, `uname`, and `cat /proc/version` on the device.
Requires `adb` on PATH and USB debugging enabled/authorised on the device.

Examples:
  python tools/android_fw.py                      # firmware summary for every device
  python tools/android_fw.py -s R58M123ABC        # one device
  python tools/android_fw.py -g bootloader -g ota # search all properties (case-insensitive)
  python tools/android_fw.py --all                # dump every property
  python tools/android_fw.py --json               # machine-readable output
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

# (label, [properties tried in order])
FIRMWARE_FIELDS = [
    ("Manufacturer", ["ro.product.manufacturer"]),
    ("Model", ["ro.product.model"]),
    ("Device", ["ro.product.device"]),
    ("Android version", ["ro.build.version.release"]),
    ("SDK level", ["ro.build.version.sdk"]),
    ("Security patch", ["ro.build.version.security_patch"]),
    ("Build ID", ["ro.build.id"]),
    ("Build display", ["ro.build.display.id"]),
    ("Build type/tags", ["ro.build.type", "ro.build.tags"]),
    ("Build date", ["ro.build.date"]),
    ("Incremental", ["ro.build.version.incremental"]),
    ("Fingerprint", ["ro.build.fingerprint"]),
    ("Bootloader", ["ro.bootloader", "ro.boot.bootloader"]),
    ("Baseband / modem", ["gsm.version.baseband", "ro.baseband"]),
    ("Radio", ["ro.build.expect.baseband"]),
    ("Vendor build", ["ro.vendor.build.fingerprint"]),
    ("Slot suffix (A/B)", ["ro.boot.slot_suffix"]),
    ("Verified boot", ["ro.boot.verifiedbootstate"]),
    ("Flash lock", ["ro.boot.flash.locked"]),
    ("OEM unlock allowed", ["sys.oem_unlock_allowed"]),
    ("Treble enabled", ["ro.treble.enabled"]),
    ("Dynamic partitions", ["ro.boot.dynamic_partitions"]),
    ("CPU ABI", ["ro.product.cpu.abi"]),
    ("Serial", ["ro.serialno"]),
]


def adb(args, serial=None, timeout=20):
    cmd = ["adb"] + (["-s", serial] if serial else []) + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or "adb failed")
    return p.stdout


def list_devices():
    out = adb(["devices"])
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


_PROP = re.compile(r"^\[(.*?)\]: \[(.*)\]$")


def parse_getprop(text):
    props = {}
    for line in text.splitlines():
        m = _PROP.match(line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def collect(serial):
    props = parse_getprop(adb(["shell", "getprop"], serial))
    try:
        kernel = adb(["shell", "cat", "/proc/version"], serial).strip()
    except RuntimeError:
        kernel = ""
    summary = {}
    for label, keys in FIRMWARE_FIELDS:
        vals = [props[k] for k in keys if props.get(k)]
        summary[label] = " / ".join(vals) if vals else "-"
    summary["Kernel"] = kernel or "-"
    return {"serial": serial, "summary": summary, "props": props}


def search(props, terms):
    """Case-insensitive match of any term against property name or value."""
    terms = [t.lower() for t in terms]
    return {
        k: v for k, v in sorted(props.items())
        if any(t in k.lower() or t in v.lower() for t in terms)
    }


def render(info, terms, dump_all):
    out = ["=" * 72, "Device: %s" % info["serial"], "=" * 72]
    if terms:
        hits = search(info["props"], terms)
        out.append("Search %s: %d match(es)" % (terms, len(hits)))
        out += ["  %s = %s" % kv for kv in hits.items()]
    elif dump_all:
        out += ["  %s = %s" % kv for kv in sorted(info["props"].items())]
    else:
        w = max(len(k) for k in info["summary"])
        out += ["  %-*s  %s" % (w, k, v) for k, v in info["summary"].items()]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-s", "--serial", help="target a specific device serial")
    ap.add_argument("-g", "--grep", action="append", default=[],
                    help="search term for property names/values (repeatable)")
    ap.add_argument("--all", action="store_true", help="dump every property")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    a = ap.parse_args(argv)

    if not shutil.which("adb"):
        print("error: adb not found on PATH (install Android platform-tools)", file=sys.stderr)
        return 2
    try:
        devices = list_devices()
    except Exception as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    ready = [s for s, st in devices if st == "device" and (not a.serial or s == a.serial)]
    for s, st in devices:
        if st != "device" and (not a.serial or s == a.serial):
            print("skipping %s: state '%s' (authorise USB debugging on the device)" % (s, st),
                  file=sys.stderr)
    if not ready:
        print("error: no ready Android device found", file=sys.stderr)
        return 2

    results, failed = [], 0
    for s in ready:
        try:
            results.append(collect(s))
        except Exception as e:
            failed += 1
            print("error: %s: %s" % (s, e), file=sys.stderr)

    if a.json:
        payload = []
        for r in results:
            item = {"serial": r["serial"], "summary": r["summary"]}
            if a.grep:
                item["matches"] = search(r["props"], a.grep)
            if a.all:
                item["props"] = r["props"]
            payload.append(item)
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            print(render(r, a.grep, a.all))
    return 1 if failed and not results else 0


if __name__ == "__main__":
    sys.exit(main())
