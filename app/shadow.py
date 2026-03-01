"""
shadow.py — реальный расчёт тени вдоль пешеходного маршрута.

Алгоритм:
1. Получаем маршруты от OSRM (до 3 альтернатив)
2. Разбиваем каждый маршрут на сегменты по ~40 м
3. Для каждого сегмента:
   a. Вычисляем азимут и высоту солнца (ephem)
   b. Скачиваем здания из Overpass API (с высотами)
   c. STRtree-индекс для быстрого поиска теней (O(n·log k) вместо O(n·k))
4. Суммируем: shade_fraction по всему маршруту
5. Для side_guidance: на каждом участке находим оптимальную сторону улицы
"""

import math
import logging
from datetime import datetime, timezone
from typing import Optional

import ephem
import httpx
import numpy as np
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

log = logging.getLogger(__name__)

# ─── Константы ────────────────────────────────────────────────────────────────
SAMPLE_STEP_M    = 40       # шаг дискретизации (метров) — увеличен для скорости
BUILDING_RADIUS  = 80       # радиус поиска зданий (метров)
SIDE_OFFSET_M    = 4.0      # смещение для проверки стороны улицы
DEFAULT_HEIGHT   = 10.0
FLOORS_DEFAULT   = 3
FLOOR_HEIGHT     = 3.2

# ─── Координатные утилиты ─────────────────────────────────────────────────────

def _deg2rad(d): return d * math.pi / 180
def _rad2deg(r): return r * 180 / math.pi

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    f1, f2 = _deg2rad(lat1), _deg2rad(lat2)
    df = _deg2rad(lat2 - lat1)
    dl = _deg2rad(lon2 - lon1)
    a = math.sin(df/2)**2 + math.cos(f1)*math.cos(f2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    f1, f2 = _deg2rad(lat1), _deg2rad(lat2)
    dl = _deg2rad(lon2 - lon1)
    x = math.sin(dl) * math.cos(f2)
    y = math.cos(f1)*math.sin(f2) - math.sin(f1)*math.cos(f2)*math.cos(dl)
    return (_rad2deg(math.atan2(x, y)) + 360) % 360

def offset_point(lat, lon, distance_m, bearing_deg_val) -> tuple:
    R = 6_371_000
    b = _deg2rad(bearing_deg_val)
    f1, l1 = _deg2rad(lat), _deg2rad(lon)
    f2 = math.asin(math.sin(f1)*math.cos(distance_m/R) +
                   math.cos(f1)*math.sin(distance_m/R)*math.cos(b))
    l2 = l1 + math.atan2(math.sin(b)*math.sin(distance_m/R)*math.cos(f1),
                          math.cos(distance_m/R)-math.sin(f1)*math.sin(f2))
    return _rad2deg(f2), _rad2deg(l2)

# ─── Положение солнца ─────────────────────────────────────────────────────────

def sun_position(lat, lon, dt: datetime) -> tuple[float, float]:
    obs = ephem.Observer()
    obs.lat  = str(lat)
    obs.lon  = str(lon)
    obs.date = dt.strftime('%Y/%m/%d %H:%M:%S')
    obs.pressure = 0
    sun = ephem.Sun(obs)
    return _rad2deg(float(sun.alt)), _rad2deg(float(sun.az))

# ─── Тень от здания ───────────────────────────────────────────────────────────

def _building_height(tags: dict) -> float:
    if 'height' in tags:
        try:
            return float(str(tags['height']).replace('m','').strip())
        except ValueError:
            pass
    if 'building:levels' in tags:
        try:
            return float(tags['building:levels']) * FLOOR_HEIGHT
        except ValueError:
            pass
    return FLOORS_DEFAULT * FLOOR_HEIGHT

def shadow_polygon(footprint: Polygon, height_m: float,
                   sun_alt_deg: float, sun_az_deg: float) -> Optional[Polygon]:
    if sun_alt_deg <= 1.0:
        return None

    shadow_len_m = min(height_m / math.tan(_deg2rad(sun_alt_deg)), 300)
    shadow_dir   = (sun_az_deg + 180) % 360
    dx = math.sin(_deg2rad(shadow_dir))
    dy = math.cos(_deg2rad(shadow_dir))

    centroid  = footprint.centroid
    lat_scale = 1 / 111_320
    lon_scale = 1 / (111_320 * math.cos(_deg2rad(centroid.y)))

    offset_lat = dy * shadow_len_m * lat_scale
    offset_lon = dx * shadow_len_m * lon_scale

    coords        = list(footprint.exterior.coords)
    shadow_coords = [(x + offset_lon, y + offset_lat) for x, y in coords]
    try:
        # convex_hull быстрее и достаточно точно
        from shapely.geometry import MultiPolygon
        combined = Polygon(coords + shadow_coords).convex_hull
        if combined.is_valid and not combined.is_empty:
            return combined
    except Exception:
        pass
    return None

# ─── Полигон дерева ───────────────────────────────────────────────────────────

def _circle_polygon(lat: float, lon: float, radius_m: float, n: int = 8) -> Optional[Polygon]:
    """Приближённый круг (n-угольник) для кроны дерева в градусах координат."""
    lat_scale = 1 / 111_320
    lon_scale = 1 / (111_320 * math.cos(_deg2rad(lat)))
    coords = [
        (lon + radius_m * lon_scale * math.cos(2 * math.pi * i / n),
         lat + radius_m * lat_scale * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    try:
        poly = Polygon(coords)
        return poly if poly.is_valid else None
    except Exception:
        return None


def _tree_height_radius(tags: dict) -> tuple[float, float]:
    """Высота и радиус кроны дерева из OSM-тегов."""
    try:
        h = float(str(tags.get("height", "8")).replace("m", "").strip())
    except (ValueError, AttributeError):
        h = 8.0
    try:
        d = float(str(tags.get("diameter_crown", "6")).replace("m", "").strip())
    except (ValueError, AttributeError):
        d = 6.0
    return h, d / 2


# ─── Дискретизация маршрута ───────────────────────────────────────────────────

def interpolate_route(coords, step_m=SAMPLE_STEP_M):
    result = []
    for i in range(len(coords) - 1):
        la1, lo1 = coords[i]
        la2, lo2 = coords[i+1]
        seg_len = haversine_m(la1, lo1, la2, lo2)
        n = max(1, int(seg_len / step_m))
        for k in range(n):
            t = k / n
            result.append((la1 + t*(la2-la1), lo1 + t*(lo2-lo1)))
    if coords:
        result.append(coords[-1])
    return result

# ─── Overpass API ─────────────────────────────────────────────────────────────

def _bbox_for_route(coords, pad=0.003):
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return (min(lats)-pad, min(lons)-pad, max(lats)+pad, max(lons)+pad)

async def fetch_buildings(coords, client: httpx.AsyncClient) -> list[dict]:
    """
    Загружает из Overpass API объекты, создающие тень:
    - здания (buildings) с высотами
    - отдельные деревья (natural=tree)
    - лесные массивы и рощи (landuse=forest, natural=wood)
    - ряды деревьев (natural=tree_row)
    Деревья значительно улучшают точность в городских парках и аллеях.
    """
    s, w, n, e = _bbox_for_route(coords)
    query = f"""
    [out:json][timeout:22];
    (
      way["building"]({s},{w},{n},{e});
      node["natural"="tree"]({s},{w},{n},{e});
      way["natural"="wood"]({s},{w},{n},{e});
      way["landuse"="forest"]({s},{w},{n},{e});
      way["natural"="tree_row"]({s},{w},{n},{e});
      relation["natural"="wood"]({s},{w},{n},{e});
    );
    out body geom;
    """
    try:
        r = await client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=25
        )
        r.raise_for_status()
        elements = r.json().get("elements", [])
        objects: list[dict] = []
        tree_count = 0  # ограничиваем кол-во отдельных деревьев для производительности

        for el in elements:
            tags    = el.get("tags", {})
            el_type = el.get("type")

            # ── Здания ────────────────────────────────────────────────────────
            if tags.get("building"):
                if el_type == "way" and "geometry" in el:
                    coords_b = [(nd["lon"], nd["lat"]) for nd in el["geometry"]]
                    if len(coords_b) >= 3:
                        try:
                            poly = Polygon(coords_b)
                            if poly.is_valid:
                                objects.append({
                                    "polygon": poly,
                                    "height":  _building_height(tags),
                                    "type":    "building"
                                })
                        except Exception:
                            pass

            # ── Отдельные деревья (node) ───────────────────────────────────
            elif tags.get("natural") == "tree" and el_type == "node":
                if tree_count >= 400:       # не более 400 деревьев для скорости
                    continue
                lat_t = el.get("lat")
                lon_t = el.get("lon")
                if lat_t is not None and lon_t is not None:
                    h, r = _tree_height_radius(tags)
                    poly = _circle_polygon(lat_t, lon_t, r)
                    if poly:
                        objects.append({"polygon": poly, "height": h, "type": "tree"})
                        tree_count += 1

            # ── Леса, рощи, ряды деревьев (way/relation) ─────────────────
            elif (tags.get("natural") in ("wood", "tree_row") or
                  tags.get("landuse") == "forest"):
                if "geometry" in el:
                    coords_b = [(nd["lon"], nd["lat"]) for nd in el["geometry"]]
                    if len(coords_b) >= 3:
                        try:
                            poly = Polygon(coords_b)
                            if poly.is_valid:
                                objects.append({
                                    "polygon": poly,
                                    "height":  12.0,   # средняя высота лесного покрова
                                    "type":    "forest"
                                })
                        except Exception:
                            pass

        log.info(f"Shadow objects: buildings={sum(1 for o in objects if o['type']=='building')}, "
                 f"trees={sum(1 for o in objects if o['type']=='tree')}, "
                 f"forests={sum(1 for o in objects if o['type']=='forest')}")
        return objects
    except Exception as e:
        log.warning(f"Overpass error: {e}")
        return []

# ─── Weather / UV ─────────────────────────────────────────────────────────────

async def fetch_weather(lat, lon, client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "uv_index,cloud_cover,apparent_temperature",
                "forecast_days": 1, "timezone": "auto"
            },
            timeout=10
        )
        hours = r.json().get("hourly", {})
        now_h = datetime.now(timezone.utc).hour
        uv    = hours.get("uv_index",            [0])[now_h] or 0
        cloud = hours.get("cloud_cover",          [0])[now_h] or 0
        temp  = hours.get("apparent_temperature", [25])[now_h] or 25
        return {"uv_index": float(uv), "cloud_cover": float(cloud), "temp_c": float(temp)}
    except Exception as e:
        log.warning(f"Weather error: {e}")
        return {"uv_index": 3.0, "cloud_cover": 30.0, "temp_c": 25.0}

# ─── Основной анализ маршрута (оптимизирован через STRtree) ──────────────────

def analyse_route(points, buildings, sun_alt, sun_az, weather,
                  walk_speed=1.35, depart_dt=None):
    if depart_dt is None:
        depart_dt = datetime.now(timezone.utc)

    # ── 1. Строим полигоны теней ОДИН РАЗ ────────────────────────────────────
    shadow_polys: list[Polygon] = []
    if sun_alt > 3:
        for b in buildings:
            sp = shadow_polygon(b["polygon"], b["height"], sun_alt, sun_az)
            if sp is not None:
                shadow_polys.append(sp)
            shadow_polys.append(b["polygon"])  # само здание тоже даёт тень

    # ── 2. STRtree-индекс для быстрого поиска ────────────────────────────────
    tree = STRtree(shadow_polys) if shadow_polys else None

    cloud_factor = max(0, 1 - weather["cloud_cover"] / 100 * 0.7)
    uv_base      = weather["uv_index"] * cloud_factor

    shade_pts  = 0
    sun_pts    = 0
    uv_total   = 0.0
    heat_total = 0.0
    side_guidance: list[dict] = []
    last_side  = None
    dist_acc   = 0.0
    shade_seq: list[bool] = []   # per-point shade status for shade_map

    for i, (lat, lon) in enumerate(points):
        pt = Point(lon, lat)

        # ── 3. Проверяем тень через индекс O(log k) ───────────────────────────
        in_shade = False
        if tree is not None:
            # query возвращает индексы кандидатов по bbox
            candidates = tree.query(pt)
            for idx in candidates:
                if shadow_polys[idx].contains(pt):
                    in_shade = True
                    break

        shade_seq.append(in_shade)   # ← запоминаем для shade_map

        if in_shade:
            shade_pts += 1
            uv_pt      = uv_base * 0.05
        else:
            sun_pts   += 1
            uv_pt      = uv_base

        uv_total   += uv_pt
        heat_total += weather["temp_c"] * (0.6 if in_shade else 1.0)

        # ── 4. Side guidance ──────────────────────────────────────────────────
        if i < len(points) - 1 and tree is not None:
            seg_b = bearing_deg(lat, lon, points[i+1][0], points[i+1][1])
            ll, rl = offset_point(lat, lon, SIDE_OFFSET_M, (seg_b - 90) % 360), \
                     offset_point(lat, lon, SIDE_OFFSET_M, (seg_b + 90) % 360)
            lp = Point(ll[1], ll[0])
            rp = Point(rl[1], rl[0])

            left_sh  = any(shadow_polys[j].contains(lp) for j in tree.query(lp))
            right_sh = any(shadow_polys[j].contains(rp) for j in tree.query(rp))

            best_side = None
            if left_sh and not right_sh:
                best_side = "left"
            elif right_sh and not left_sh:
                best_side = "right"

            if best_side and best_side != last_side:
                seg_dist = int(dist_acc)
                if last_side is not None:
                    side_guidance.append({
                        "at_m": seg_dist, "action": "switch_side",
                        "preferred_side": best_side,
                        "note": f"Перейдите на {'левую' if best_side == 'left' else 'правую'} сторону"
                    })
                else:
                    side_guidance.append({"from_m": 0, "preferred_side": best_side})
                last_side = best_side

        if i > 0:
            dist_acc += haversine_m(points[i-1][0], points[i-1][1], lat, lon)

    total_pts  = shade_pts + sun_pts or 1
    total_m    = dist_acc
    duration_s = total_m / walk_speed
    shade_frac = shade_pts / total_pts
    shade_min  = duration_s * shade_frac / 60
    sun_min    = duration_s * (1 - shade_frac) / 60
    avg_uv     = uv_total / total_pts
    avg_heat   = heat_total / total_pts

    if not buildings:
        confidence = "low"
    elif sun_alt < 10:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "shade_fraction": shade_frac,
        "shade_min":      round(shade_min, 1),
        "sun_min":        round(sun_min,   1),
        "uv_dose":        round(avg_uv * duration_s / 3600, 2),
        "heat_load":      round(avg_heat * duration_s / 3600, 2),
        "temp_feels_c":   round(avg_heat, 1),
        "confidence":     confidence,
        "side_guidance":  side_guidance[:10],
        "distance_m":     int(total_m),
        "duration_s":     int(duration_s),
        "shade_map":      ''.join('1' if s else '0' for s in shade_seq),
    }
