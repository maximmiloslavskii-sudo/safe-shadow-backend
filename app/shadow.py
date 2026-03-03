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

Маршрут 3 (максимальная тень) строится через граф улиц OSM:
- Каждому отрезку улицы назначается стоимость: dist × (1 + P × (1 − shade))
- Dijkstra ищет путь с минимальной стоимостью (= максимальной тенью)
- Параметры тени вычисляются из физики: солнечная геометрия + геометрия застройки
"""

import itertools
import math
import logging
import time
from datetime import datetime, timezone
from heapq import heappush, heappop
from typing import Optional

import ephem
import httpx
import numpy as np
from scipy.spatial import cKDTree
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

# ─── Overpass API: здания ──────────────────────────────────────────────────────

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
        tree_count = 0

        for el in elements:
            tags    = el.get("tags", {})
            el_type = el.get("type")

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

            elif tags.get("natural") == "tree" and el_type == "node":
                if tree_count >= 400:
                    continue
                lat_t = el.get("lat")
                lon_t = el.get("lon")
                if lat_t is not None and lon_t is not None:
                    h, r = _tree_height_radius(tags)
                    poly = _circle_polygon(lat_t, lon_t, r)
                    if poly:
                        objects.append({"polygon": poly, "height": h, "type": "tree"})
                        tree_count += 1

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
                                    "height":  12.0,
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

# ─── Построение полигонов теней (переиспользуется в graph routing) ────────────

def build_shadow_polys(buildings: list, sun_alt: float, sun_az: float) -> list:
    """
    Строит список полигонов теней (тень + контур здания) из списка объектов.
    Используется как в analyse_route, так и в find_shade_route.
    """
    polys: list[Polygon] = []
    if sun_alt > 3:
        for b in buildings:
            sp = shadow_polygon(b["polygon"], b["height"], sun_alt, sun_az)
            if sp is not None:
                polys.append(sp)
            polys.append(b["polygon"])
    return polys

# ─── Основной анализ маршрута (оптимизирован через STRtree) ──────────────────

def analyse_route(points, buildings, sun_alt, sun_az, weather,
                  walk_speed=1.35, depart_dt=None):
    if depart_dt is None:
        depart_dt = datetime.now(timezone.utc)

    shadow_polys = build_shadow_polys(buildings, sun_alt, sun_az)
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
    shade_seq: list[bool] = []

    for i, (lat, lon) in enumerate(points):
        pt = Point(lon, lat)

        in_shade = False
        if tree is not None:
            candidates = tree.query(pt)
            for idx in candidates:
                if shadow_polys[idx].contains(pt):
                    in_shade = True
                    break

        shade_seq.append(in_shade)

        if in_shade:
            shade_pts += 1
            uv_pt      = uv_base * 0.05
        else:
            sun_pts   += 1
            uv_pt      = uv_base

        uv_total   += uv_pt
        heat_total += weather["temp_c"] * (0.6 if in_shade else 1.0)

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

# ─── Overpass API: сеть улиц для графового маршрутизатора ────────────────────

async def fetch_street_network(
    s: float, w: float, n: float, e: float,
    client: httpx.AsyncClient
) -> list[tuple[float, float, float, float]]:
    """
    Загружает пешеходную сеть улиц в bbox (s, w, n, e) из Overpass.
    Возвращает список отрезков (lat1, lon1, lat2, lon2).

    Включает все проходимые типы: жилые улицы, тротуары, пешеходные зоны,
    парковые дорожки, сервисные проезды и т.д.
    Исключает: автомагистрали, строящиеся/предложенные/заброшенные дороги.
    """
    # Только пешеходные/жилые типы (не все highway)
    query = f"""[out:json][timeout:25];
way["highway"~"footway|path|pedestrian|living_street|residential|service|track|steps|unclassified|tertiary|secondary|primary"]
   ({s:.5f},{w:.5f},{n:.5f},{e:.5f});
out body geom;"""
    try:
        r = await client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=28,
        )
        r.raise_for_status()
        segs: list[tuple[float, float, float, float]] = []
        for el in r.json().get("elements", []):
            geo = el.get("geometry", [])
            for i in range(len(geo) - 1):
                a, b = geo[i], geo[i + 1]
                segs.append((a["lat"], a["lon"], b["lat"], b["lon"]))
        log.info(f"Street segments fetched: {len(segs)}")
        return segs
    except Exception as exc:
        log.warning(f"Street network fetch error: {exc}")
        return []

# ─── Физическая оценка тени по геометрии застройки ───────────────────────────

def _build_building_index(buildings: list):
    """
    Строит быстрый пространственный индекс зданий (cKDTree по центроидам).
    Возвращает (kdtree | None, lat_arr, lon_arr, heights_arr).
    Используется для O(log N) поиска ближайших зданий вместо O(N) линейного.
    """
    if not buildings:
        return None, np.empty(0), np.empty(0), np.empty(0)
    lats, lons, heights = [], [], []
    for b in buildings:
        try:
            c = b["polygon"].centroid
            lats.append(c.y)
            lons.append(c.x)
            heights.append(b["height"])
        except Exception:
            pass
    if not lats:
        return None, np.empty(0), np.empty(0), np.empty(0)
    lat_arr = np.array(lats)
    lon_arr = np.array(lons)
    h_arr   = np.array(heights)
    # Приводим к локальным декартовым координатам (метры) для KDTree
    lat0, lon0 = lat_arr.mean(), lon_arr.mean()
    cos_lat = math.cos(math.radians(lat0))
    y = (lat_arr - lat0) * 111_320
    x = (lon_arr - lon0) * 111_320 * cos_lat
    kd = cKDTree(np.column_stack([y, x]))
    # Сохраняем ref-данные для обратного пересчёта
    return (kd, lat0, lon0, cos_lat), lat_arr, lon_arr, h_arr


def _urban_canyon_shade_fast(
    la1: float, lo1: float, la2: float, lo2: float,
    bld_index,           # кортеж (kd, lat0, lon0, cos_lat) или None
    bld_heights: np.ndarray,
    sun_alt_deg: float,
    sun_az_deg: float,
    search_r_m: float = 50.0,
) -> float:
    """
    Оценивает долю тени на сегменте улицы через физику урбан-каньона.

    Использует:
    - Высоту солнца (solar altitude) → длина тени от здания
    - Азимут солнца vs ориентация улицы → поперечная компонента тени
    - Среднюю высоту H ближайших зданий и оценку ширины улицы W
    - H/W ratio → покрытие тротуара тенью

    Физика (Sky View Factor / Urban Canyon):
        shadow_reach = H / tan(sun_alt)
        cross_shadow = shadow_reach × |sin(sun_az − street_normal)|
        shade_frac   = clip(cross_shadow / (W/2), 0, 1)

    Ускорена через cKDTree: O(log N) вместо O(N) по зданиям.
    """
    if sun_alt_deg <= 1.0:
        return 0.95  # Ночь/рассвет — считаем в тени

    # Если нет данных о зданиях → используем типичную городскую застройку.
    # Это гарантирует наличие градиента по ориентации улицы даже без OSM-высот.
    if bld_index is None or len(bld_heights) == 0:
        # Fallback: предполагаем средний городской каньон H=12м, W=14м
        avg_H = 12.0
        W = 14.0
        seg_bear = bearing_deg(la1, lo1, la2, lo2)
        street_normal = (seg_bear + 90) % 360
        delta = ((sun_az_deg - street_normal + 360) % 360)
        if delta > 180:
            delta = 360 - delta
        shadow_len   = avg_H / math.tan(math.radians(max(sun_alt_deg, 3)))
        cross_shadow = shadow_len * abs(math.sin(math.radians(delta)))
        return min(cross_shadow / max(W / 2, 1.0), 1.0)

    kd, lat0, lon0, cos_lat = bld_index
    mid_lat = (la1 + la2) / 2
    mid_lon = (lo1 + lo2) / 2
    seg_bear = bearing_deg(la1, lo1, la2, lo2)

    # Локальные метрические координаты центра сегмента
    my = (mid_lat - lat0) * 111_320
    mx = (mid_lon - lon0) * 111_320 * cos_lat

    # Поиск ближайших зданий в радиусе search_r_m
    idxs = kd.query_ball_point([my, mx], r=search_r_m)
    if not idxs:
        return 0.0

    nearby_h = bld_heights[idxs]
    avg_H = float(nearby_h.mean())
    n = len(idxs)

    # Оценка ширины улицы по кол-ву зданий (больше → плотнее застройка)
    W = 8.0 if n >= 4 else (12.0 if n >= 2 else 16.0)

    # Нормаль к улице
    street_normal = (seg_bear + 90) % 360
    delta = ((sun_az_deg - street_normal + 360) % 360)
    if delta > 180:
        delta = 360 - delta

    # Длина тени и её поперечная компонента
    shadow_len   = avg_H / math.tan(math.radians(max(sun_alt_deg, 3)))
    cross_shadow = shadow_len * abs(math.sin(math.radians(delta)))

    return min(cross_shadow / max(W / 2, 1.0), 1.0)

# ─── Графовый маршрутизатор: максимум тени ────────────────────────────────────

def _shade_at_pt(
    lat: float, lon: float,
    shadow_polys: list,
    tree_idx: Optional[STRtree],
) -> float:
    """Возвращает 1.0 если точка в тени, иначе 0.0."""
    if tree_idx is None:
        return 0.0
    pt = Point(lon, lat)
    for idx in tree_idx.query(pt):
        if shadow_polys[idx].contains(pt):
            return 1.0
    return 0.0


def _edge_shade_score(
    la1: float, lo1: float, la2: float, lo2: float,
    shadow_polys: list,
    poly_tree: Optional[STRtree],
    bld_index,           # (kd, lat0, lon0, cos_lat) или None — для быстрой физики
    bld_heights: np.ndarray,
    sun_alt: float,
    sun_az: float,
) -> float:
    """
    Вычисляет долю тени на отрезке улицы (от 0.0 до 1.0).

    Два метода, берём максимум:
    1. Полигонная проверка центра сегмента (O(log K) через STRtree)
    2. Физика урбан-каньона через cKDTree зданий (O(log N))
    """
    # Метод 1: полигонная проверка (только середина сегмента — один запрос)
    poly_shade = _shade_at_pt(
        (la1 + la2) / 2, (lo1 + lo2) / 2, shadow_polys, poly_tree
    ) if shadow_polys else 0.0

    # Метод 2: физика урбан-каньона с быстрым KDTree-индексом
    physics_shade = _urban_canyon_shade_fast(
        la1, lo1, la2, lo2, bld_index, bld_heights, sun_alt, sun_az
    ) if sun_alt > 1.0 else 0.0

    return max(poly_shade, physics_shade)


def find_shade_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    street_segs: list[tuple[float, float, float, float]],
    shadow_polys: list,
    buildings: list,
    sun_alt: float,
    sun_az: float,
    max_detour: float = 2.5,
    sun_penalty: float = 3.0,
) -> Optional[tuple[list[tuple[float, float]], float]]:
    """
    Строит маршрут с максимальной тенью через взвешенный граф улиц.

    Параметры весов рёбер:
        cost = distance_m × (1 + sun_penalty × (1 − shade_fraction))
        - Полностью в тени:  cost = distance_m × 1.0
        - Полностью на солнце: cost = distance_m × (1 + sun_penalty)
        sun_penalty=3.0 → солнечный маршрут в 4× «дороже» теневого.

    Физические параметры тени:
        - solar altitude/azimuth → длина тени здания
        - H/W ratio → Urban Canyon Index
        - Sky View Factor (shadow_reach / street_width)
        - Теневые полигоны от зданий/деревьев (STRtree)
        - Пространственный cKDTree по зданиям для O(log N) поиска

    max_detour: максимально допустимая длина пути / прямое расстояние.
    Возвращает: ([(lat, lon), ...], total_dist_m) или None при неудаче.
    """
    if not street_segs:
        return None

    # ── Пространственные индексы ──────────────────────────────────────────────
    poly_tree: Optional[STRtree] = STRtree(shadow_polys) if shadow_polys else None

    # Быстрый KDTree по центроидам зданий для _urban_canyon_shade_fast
    bld_index_tuple, _, _, bld_heights = _build_building_index(buildings)

    # ── Строим граф смежности ─────────────────────────────────────────────────
    # Узел = (lat, lon) с точностью до 5 знаков (~1 м)
    PREC = 5
    adj: dict[tuple, list] = {}           # node → [(neighbor, dist_m, edge_cost)]
    node_coords: dict[tuple, tuple] = {}  # node_id → (lat, lon)

    def _nid(lat: float, lon: float) -> tuple:
        return (round(lat, PREC), round(lon, PREC))

    for la1, lo1, la2, lo2 in street_segs:
        d_m = haversine_m(la1, lo1, la2, lo2)
        if d_m < 0.5:
            continue
        n1, n2 = _nid(la1, lo1), _nid(la2, lo2)
        node_coords[n1] = (la1, lo1)
        node_coords[n2] = (la2, lo2)

        shade = _edge_shade_score(
            la1, lo1, la2, lo2,
            shadow_polys, poly_tree,
            bld_index_tuple, bld_heights,
            sun_alt, sun_az,
        )
        # Стоимость: минимизируем пребывание на солнце
        cost = d_m * (1.0 + sun_penalty * (1.0 - shade))

        adj.setdefault(n1, []).append((n2, d_m, cost))
        adj.setdefault(n2, []).append((n1, d_m, cost))  # двунаправленный граф

    if not adj:
        return None

    all_nodes = list(adj.keys())
    if len(all_nodes) < 2:
        return None

    # ── Привязка истока и стока через numpy (O(N) но быстро) ─────────────────
    node_arr = np.array([(node_coords[n][0], node_coords[n][1]) for n in all_nodes])

    def _nearest(lat: float, lon: float) -> tuple:
        diffs = node_arr - np.array([lat, lon])
        idx = int(np.argmin((diffs ** 2).sum(axis=1)))
        return all_nodes[idx]

    start = _nearest(origin_lat, origin_lon)
    goal  = _nearest(dest_lat, dest_lon)

    if start == goal:
        return None

    direct_dist  = haversine_m(origin_lat, origin_lon, dest_lat, dest_lon)
    max_dist_m   = direct_dist * max_detour

    start_snap = haversine_m(origin_lat, origin_lon, node_coords[start][0], node_coords[start][1])
    goal_snap  = haversine_m(dest_lat, dest_lon, node_coords[goal][0], node_coords[goal][1])
    log.info(
        f"Graph: {len(adj)} nodes | direct={direct_dist:.0f}m | "
        f"start_snap={start_snap:.0f}m | goal_snap={goal_snap:.0f}m | budget={max_dist_m:.0f}m"
    )

    # ── Внутренний Dijkstra (минимизация sun-weighted стоимости) ──────────────
    def _run_dijkstra(
        src: tuple, dst: tuple, budget_m: float, deadline: float
    ) -> Optional[tuple[list, float, float]]:
        """
        Dijkstra от src до dst с ограничением по дистанции budget_m.
        Возвращает (path_coords, total_dist_m, total_shade_dist_m) или None.
        """
        INF = float("inf")
        g_cost: dict[tuple, float] = {src: 0.0}
        g_dist: dict[tuple, float] = {src: 0.0}
        g_shade: dict[tuple, float] = {src: 0.0}  # накопленные метры в тени
        came: dict[tuple, Optional[tuple]] = {src: None}
        ctr = itertools.count()
        pq  = [(0.0, next(ctr), src)]

        while pq:
            if time.monotonic() > deadline:
                log.warning("Shade Dijkstra timeout")
                break
            c, _, node = heappop(pq)
            if c > g_cost.get(node, INF) + 1e-9:
                continue
            if node == dst:
                break
            cur_dist = g_dist[node]
            for nb, edge_dist, edge_cost in adj.get(node, []):
                new_dist = cur_dist + edge_dist
                if new_dist > budget_m:
                    continue
                new_cost = c + edge_cost
                if new_cost < g_cost.get(nb, INF):
                    g_cost[nb]  = new_cost
                    g_dist[nb]  = new_dist
                    # shade накапливаем пропорционально shade_frac ребра
                    # shade_frac ≈ 1 - edge_cost / (dist * (1 + sun_penalty))
                    shade_frac_approx = 1.0 - (edge_cost - edge_dist) / max(
                        edge_dist * sun_penalty, 1e-6
                    )
                    g_shade[nb] = g_shade[node] + edge_dist * max(shade_frac_approx, 0.0)
                    came[nb]    = node
                    heappush(pq, (new_cost, next(ctr), nb))

        if dst not in came:
            return None
        path: list[tuple[float, float]] = []
        n: Optional[tuple] = dst
        while n is not None:
            path.append(node_coords[n])
            n = came[n]
        path.reverse()
        if len(path) < 2:
            return None
        return path, g_dist[dst], g_shade.get(dst, 0.0)

    deadline = time.monotonic() + 15.0

    # ── Вариант A: прямой маршрут (origin → destination) ──────────────────────
    result_a = _run_dijkstra(start, goal, max_dist_m, deadline)
    if result_a is None:
        # Fallback: retry with no distance cap — catches disconnected-looking graphs
        # where the snap adds enough extra distance to exceed budget
        log.warning(
            f"Dijkstra returned None with budget {max_dist_m:.0f}m — retrying unlimited"
        )
        result_a = _run_dijkstra(start, goal, float("inf"), deadline)
        if result_a is None:
            log.warning("Dijkstra failed even without budget — graph may be disconnected")

    # ── Вариант B: маршрут через путевую точку (крюк) ─────────────────────────
    # Смещаем mid-точку перпендикулярно прямому маршруту на 35% direct_dist.
    # Выбираем сторону, перпендикулярную солнцу (теневая сторона улицы).
    # Это ГАРАНТИРУЕТ изучение других улиц, не только самого прямого пути.
    result_b: Optional[tuple] = None
    if direct_dist > 150:   # крюк бессмысленен для очень коротких маршрутов
        try:
            direct_bear = bearing_deg(origin_lat, origin_lon, dest_lat, dest_lon)
            mid_lat = (origin_lat + dest_lat) / 2
            mid_lon = (origin_lon + dest_lon) / 2
            displace = min(direct_dist * 0.35, 500)  # не более 500м в сторону

            # Пробуем оба направления перпендикуляра и берём более теневой узел
            wp_candidates = [
                offset_point(mid_lat, mid_lon, displace, (direct_bear + 90)  % 360),
                offset_point(mid_lat, mid_lon, displace, (direct_bear - 90 + 360) % 360),
            ]
            # Выбираем кандидата ближе к теневой стороне от солнца
            best_wp = max(
                wp_candidates,
                key=lambda wp: _shade_at_pt(wp[0], wp[1], shadow_polys, poly_tree)
                               + _urban_canyon_shade_fast(
                                   wp[0] - 0.0001, wp[1], wp[0] + 0.0001, wp[1],
                                   bld_index_tuple, bld_heights, sun_alt, sun_az
                               )
            )
            wp_node = _nearest(best_wp[0], best_wp[1])

            if wp_node not in (start, goal):
                # Оставшийся бюджет после первого отрезка
                half_budget = max_dist_m
                r1 = _run_dijkstra(start, wp_node, half_budget, deadline)
                if r1 is not None:
                    path1, d1, shd1 = r1
                    r2 = _run_dijkstra(wp_node, goal, half_budget - d1, deadline)
                    if r2 is not None:
                        path2, d2, shd2 = r2
                        combined_path = path1 + path2[1:]  # избегаем дублирования wp_node
                        result_b = (combined_path, d1 + d2, shd1 + shd2)
                        log.info(f"Waypoint route: {int(d1+d2)}m via node near "
                                 f"({best_wp[0]:.4f},{best_wp[1]:.4f})")
        except Exception as exc:
            log.warning(f"Waypoint route error: {exc}")

    # ── Выбираем вариант: приоритет у маршрута через крюк ────────────────────
    def _shade_fraction(result) -> float:
        if result is None:
            return -1.0
        _, dist, shade_dist = result
        return shade_dist / max(dist, 1.0)

    sf_a = _shade_fraction(result_a)
    sf_b = _shade_fraction(result_b)

    log.info(
        f"Shade options: direct={result_a[1] if result_a else 'None'}m shade={sf_a:.1%} | "
        f"waypoint={result_b[1] if result_b else 'None'}m shade={sf_b:.1%}"
    )

    if result_a is None and result_b is None:
        log.info("Shade route: no path found in graph")
        return None

    # Приоритет отдаём маршруту через путевую точку (крюк) —
    # он ГАРАНТИРОВАННО проходит по другим улицам.
    # Используем прямой Dijkstra только если крюк не построился.
    if result_b is not None:
        chosen = result_b
        log.info("Using WAYPOINT shade route (explores different streets)")
    else:
        chosen = result_a
        log.info("Using DIRECT shade route (waypoint failed)")

    path, total_dist, _ = chosen
    return path, total_dist
