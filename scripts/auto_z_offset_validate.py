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


def get_gcode_store(count=400):
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


def wait_for_target(kind, target, tol=0.8, soak=15, timeout=1200):
    start = time.time()
    reached_at = None
    last = None
    key = "bed_temp" if kind == "bed" else "ext_temp"
    label = "Bed" if kind == "bed" else "Hotend"
    while time.time() - start < timeout:
        t = get_temps()
        last = t
        ok = t[key] >= (target - tol)
        if ok:
            if reached_at is None:
                reached_at = time.time()
            elif time.time() - reached_at >= soak:
                return t
        else:
            reached_at = None
        time.sleep(2)
    raise RuntimeError(f"Timeout esperando {label} estable. {label}={last[key]}/{target}")


def collect_new_messages(before_msgs, count=400):
    after = get_gcode_store(count)
    after_msgs = after.get("result", {}).get("gcode_store", [])
    before_keys = {(m.get("time"), m.get("message", "")) for m in before_msgs}
    return [m.get("message", "") for m in after_msgs if (m.get("time"), m.get("message", "")) not in before_keys]


def extract_z_offset(text):
    patterns = [
        r'bltouch:\s*z_offset:\s*([-+]?[0-9]*\.?[0-9]+)',
        r'z_offset\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)',
        r'offset\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def send_and_capture_value(cmd, sleep_s=2.0, polls=40, poll_sleep=1.0):
    before = get_gcode_store(400)
    before_msgs = before.get("result", {}).get("gcode_store", [])
    post_gcode(cmd, timeout=180)
    time.sleep(sleep_s)
    last_seen = []
    for _ in range(polls):
        new_texts = collect_new_messages(before_msgs, 400)
        if new_texts:
            last_seen = new_texts[-8:]
        for text in reversed(new_texts):
            value = extract_z_offset(text)
            if value is not None:
                return value, text
        time.sleep(poll_sleep)
    detail = " | ".join(last_seen) if last_seen else "sin mensajes nuevos detectables"
    raise RuntimeError(f"No se pudo capturar el resultado de: {cmd}. Ultimos mensajes: {detail}")


def run_measurement_set(points, pause_between=2.0):
    values = []
    raw = []
    for i, (x, y) in enumerate(points, start=1):
        post_gcode(f"M117 Probe {i}/{len(points)}")
        post_gcode(f"G1 X{x:.2f} Y{y:.2f} F9000")
        time.sleep(1.0)
        val, txt = send_and_capture_value("PRTOUCH_PROBE_ZOFFSET")
        values.append(val)
        raw.append(f"P({x:.2f},{y:.2f}) {txt}")
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


def home_and_park(x, y, z):
    post_gcode("G90")
    post_gcode("G28", timeout=120)
    post_gcode(f"G1 Z{z:.2f} F600")
    post_gcode(f"G1 X{x:.2f} Y{y:.2f} F9000")
    time.sleep(2)


def parse_points(txt):
    pts = []
    for chunk in txt.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        x, y = chunk.split(",")
        pts.append((float(x), float(y)))
    return pts


def default_points(cx, cy, dx, dy, x_min, x_max, y_min, y_max):
    raw = [
        (cx, cy),
        (cx - dx, cy - dy),
        (cx + dx, cy + dy),
        (cx - dx, cy + dy),
        (cx + dx, cy - dy),
    ]
    out = []
    for x, y in raw:
        x = min(max(x, x_min), x_max)
        y = min(max(y, y_min), y_max)
        out.append((x, y))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bed", type=int, required=True)
    ap.add_argument("--hotend", type=int, default=165)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-range", type=float, default=0.05)
    ap.add_argument("--retry-sets", type=int, default=2)
    ap.add_argument("--bed-soak", type=int, default=30)
    ap.add_argument("--hotend-soak", type=int, default=8)
    ap.add_argument("--park-x", type=float, default=137.75)
    ap.add_argument("--park-y", type=float, default=131.5)
    ap.add_argument("--park-z", type=float, default=10.0)
    ap.add_argument("--probe-dx", type=float, default=18.0)
    ap.add_argument("--probe-dy", type=float, default=18.0)
    ap.add_argument("--probe-points", type=str, default="")
    ap.add_argument("--min-x", type=float, default=30.0)
    ap.add_argument("--max-x", type=float, default=203.75)
    ap.add_argument("--min-y", type=float, default=30.0)
    ap.add_argument("--max-y", type=float, default=210.0)
    args = ap.parse_args()

    if args.runs < 3:
        raise SystemExit("Usa al menos --runs 3; recomendado 5")

    points = parse_points(args.probe_points) if args.probe_points else default_points(args.park_x, args.park_y, args.probe_dx, args.probe_dy, args.min_x, args.max_x, args.min_y, args.max_y)
    if len(points) < args.runs:
        repeats = (args.runs + len(points) - 1) // len(points)
        points = (points * repeats)[:args.runs]
    else:
        points = points[:args.runs]

    try:
        post_gcode("M117 Auto Z Start")
        post_gcode(f'RESPOND MSG="Calentando cama a {args.bed}C"')
        post_gcode(f"M140 S{args.bed}")
        wait_for_target("bed", args.bed, soak=args.bed_soak)

        post_gcode('RESPOND MSG="Homing y posicionando cabezal"')
        home_and_park(args.park_x, args.park_y, args.park_z)

        post_gcode(f'RESPOND MSG="Calentando hotend a {args.hotend}C"')
        post_gcode(f"M104 S{args.hotend}")
        wait_for_target("hotend", args.hotend, soak=args.hotend_soak)

        results = []
        passed = None
        for attempt in range(1, args.retry_sets + 1):
            post_gcode(f"M117 Z test {attempt}/{args.retry_sets}")
            home_and_park(args.park_x, args.park_y, args.park_z)
            values, raw = run_measurement_set(points)
            summary_all = summarize_all(values)
            summary_trim = summarize_trimmed(values)
            result = {"attempt": attempt, "points": points, "raw": raw, **summary_all, **summary_trim}
            results.append(result)
            post_gcode(f'M117 Rng {result["used_range"]:.3f}')
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
        post_gcode("PRTOUCH_PROBE_ZOFFSET APPLY_Z_ADJUST=1 CLEAR_NOZZLE=1", timeout=180)
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
