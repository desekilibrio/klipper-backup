#!/usr/bin/env python3
import argparse
import json
import re
import statistics
import sys
import time
from urllib import request, parse

MOONRAKER = "http://127.0.0.1:7125"

def post_gcode(script_text, timeout=30):
    data = parse.urlencode({"script": script_text}).encode()
    req = request.Request(f"{MOONRAKER}/printer/gcode/script", data=data, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def get_query(path, timeout=15):
    with request.urlopen(f"{MOONRAKER}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def get_gcode_store(count=300):
    return get_query(f"/server/gcode_store?count={count}")

def get_temps():
    data = get_query("/printer/objects/query?heater_bed&extruder")
    status = data.get("result", {}).get("status", {})
    bed = status.get("heater_bed", {})
    ext = status.get("extruder", {})
    return {
        "bed_temp": float(bed.get("temperature", 0)),
        "bed_target": float(bed.get("target", 0)),
        "ext_temp": float(ext.get("temperature", 0)),
        "ext_target": float(ext.get("target", 0)),
    }

def wait_for_bed(bed_target, tol=0.8, stable_s=15, timeout=1800):
    start = time.time()
    reached_at = None
    last = None
    while time.time() - start < timeout:
        t = get_temps()
        last = t
        bed_ok = t["bed_temp"] >= (bed_target - tol)
        if bed_ok:
            if reached_at is None:
                reached_at = time.time()
            elif time.time() - reached_at >= stable_s:
                return t
        else:
            reached_at = None
        time.sleep(2)
    raise RuntimeError(
        f"Timeout esperando cama estable. Bed={last['bed_temp']}/{bed_target}"
    )

def wait_for_hotend(hotend_target, tol=0.8, stable_s=15, timeout=1800):
    start = time.time()
    reached_at = None
    last = None
    while time.time() - start < timeout:
        t = get_temps()
        last = t
        ext_ok = t["ext_temp"] >= (hotend_target - tol)
        if ext_ok:
            if reached_at is None:
                reached_at = time.time()
            elif time.time() - reached_at >= stable_s:
                return t
        else:
            reached_at = None
        time.sleep(2)
    raise RuntimeError(
        f"Timeout esperando hotend estable. Hotend={last['ext_temp']}/{hotend_target}"
    )

def soak_bed(seconds):
    if seconds <= 0:
        return
    end = time.time() + seconds
    announced = set()
    while True:
        remain = int(end - time.time())
        if remain <= 0:
            break
        if remain not in announced and (remain <= 10 or remain % 60 == 0):
            post_gcode(f"M117 Bed soak {remain}s")
            announced.add(remain)
        time.sleep(1)


def send_and_capture_value(cmd, sleep_s=2.0, polls=25):
    before = get_gcode_store(300)
    before_msgs = before.get("result", {}).get("gcode_store", [])
    before_texts = [m.get("message", "") for m in before_msgs]

    post_gcode(cmd)
    time.sleep(sleep_s)

    for _ in range(polls):
        after = get_gcode_store(300)
        after_msgs = after.get("result", {}).get("gcode_store", [])
        new_msgs = [m.get("message", "") for m in after_msgs if m.get("message", "") not in before_texts]

        for text in reversed(new_msgs):
            m = re.search(r'bltouch:\s*z_offset:\s*([-+]?[0-9]*\.?[0-9]+)', text, re.IGNORECASE)
            if m:
                return float(m.group(1)), text

        time.sleep(0.8)

    raise RuntimeError(f"No se pudo capturar el resultado bltouch de: {cmd}")

def run_measurement_set(runs, material=None, pause_between=2.0):
    values = []
    raw = []
    cmd = "PRTOUCH_PROBE_ZOFFSET"
    if material:
        cmd += f" MATERIAL={material.upper()}"
    for _ in range(runs):
        val, txt = send_and_capture_value(cmd)
        values.append(val)
        raw.append(txt)
        time.sleep(pause_between)
    return values, raw

def summarize_all(values):
    return {
        "values": values,
        "min": min(values),
        "max": max(values),
        "range": max(values) - min(values),
        "mean": sum(values) / len(values),
        "median": statistics.median(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }

def summarize_trimmed(values):
    if len(values) < 2:
        raise RuntimeError("No hay suficientes muestras para descartar la primera")

    trimmed = values[1:]
    return {
        "used_values": trimmed,
        "used_min": min(trimmed),
        "used_max": max(trimmed),
        "used_range": max(trimmed) - min(trimmed),
        "used_mean": sum(trimmed) / len(trimmed),
        "used_median": statistics.median(trimmed),
        "used_stdev": statistics.pstdev(trimmed) if len(trimmed) > 1 else 0.0,
        "discarded_first": values[0],
    }

def home_and_settle():
    post_gcode("G90")
    post_gcode("G28", timeout=120)
    time.sleep(3)

def build_probe_cmd(material=None, apply=False, clear=False):
    parts = ["PRTOUCH_PROBE_ZOFFSET"]
    if apply:
        parts.append("APPLY_Z_ADJUST=1")
    if clear:
        parts.append("CLEAR_NOZZLE=1")
    if material:
        parts.append(f"MATERIAL={material.upper()}")
    return " ".join(parts)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bed", type=int, required=True)
    ap.add_argument("--hotend", type=int, required=True)
    ap.add_argument("--material", type=str, default=None)
    ap.add_argument("--bed-soak", type=int, default=0)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-range", type=float, default=0.05)
    ap.add_argument("--retry-sets", type=int, default=2)
    args = ap.parse_args()

    if args.runs < 3:
        raise SystemExit("Usa al menos --runs 3; recomendado 5")

    material = args.material.upper() if args.material else None

    try:
        post_gcode("M117 Auto Z Start")
        post_gcode(f"M140 S{args.bed}")
        post_gcode(f"M117 Bed {args.bed}C")
        wait_for_bed(args.bed)

        if args.bed_soak > 0:
            post_gcode(f"M117 Bed soak {args.bed_soak}s")
            soak_bed(args.bed_soak)

        post_gcode("M117 Homing")
        home_and_settle()

        post_gcode(f"M104 S{args.hotend}")
        post_gcode(f"M117 Hotend {args.hotend}C")
        wait_for_hotend(args.hotend)

        results = []
        passed = None

        for attempt in range(1, args.retry_sets + 1):
            post_gcode(f"M117 Z test {attempt}/{args.retry_sets}")
            values, raw = run_measurement_set(args.runs, material=material)
            summary_all = summarize_all(values)
            summary_trim = summarize_trimmed(values)

            result = {
                "attempt": attempt,
                "raw": raw,
                **summary_all,
                **summary_trim
            }
            results.append(result)

            post_gcode(f"M117 Rng {result['used_range']:.3f}")

            if result["used_range"] <= args.max_range:
                passed = result
                break

            time.sleep(5)

        if passed is None:
            msg = (
                f"Auto Z abortado: sin consistencia tras {args.retry_sets} intentos. "
                f"Ultimo rango_util={results[-1]['used_range']:.4f} "
                f"valores_utiles={results[-1]['used_values']}"
            )
            post_gcode(f'RESPOND TYPE=error MSG="{msg}"')
            print(json.dumps({"ok": False, "attempts": results}, ensure_ascii=False))
            sys.exit(2)

        post_gcode("PRTOUCH_ACCURACY SAMPLES=10 PROBE_SPEED=1", timeout=180)
        post_gcode(build_probe_cmd(material=material, apply=True, clear=True), timeout=180)
        post_gcode("SAVE_CONFIG", timeout=60)

        print(json.dumps({"ok": True, "selected": passed, "attempts": results}, ensure_ascii=False))

    except Exception as e:
        try:
            msg = str(e).replace('"', "'")
            post_gcode(f'RESPOND TYPE=error MSG="Auto Z fallo: {msg}"')
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    main()
