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
from datetime import datetime, timezone, timedelta
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
SIDE_OFFSET_M    = 1.5      # смещение для проверки стороны улицы (≈ позиция пешехода у стены)
DEFAULT_HEIGHT   = 10.0
FLOORS_DEFAULT   = 5        # типичная многоэтажка в плотной застройке (BA, LATAM)
FLOOR_HEIGHT     = 3.2
SHADOW_LEN_CAP_M = 500      # макс. длина тени (м) — увеличен для низкого солнца
SUN_RECALC_INTERVAL_S = 300 # пересчёт позиции солнца каждые 5 мин ходьбы
MAX_TREES        = 400       # макс. деревьев — берём ближайшие к маршруту

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

    shadow_len_m = min(height_m / math.tan(_deg2rad(sun_alt_deg)), SHADOW_LEN_CAP_M)
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
        pending_trees: list[dict] = []   # все деревья до сортировки

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
                lat_t = el.get("lat")
                lon_t = el.get("lon")
                if lat_t is not None and lon_t is not None:
                    h, r = _tree_height_radius(tags)
                    poly = _circle_polygon(lat_t, lon_t, r)
                    if poly:
                        pending_trees.append({
                            "polygon": poly, "height": h, "type": "tree",
                            "_lat": lat_t, "_lon": lon_t,
                        })

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

        # Сортируем деревья по расстоянию до центра маршрута, берём MAX_TREES ближайших
        if pending_trees:
            route_lat = sum(c[0] for c in coords) / len(coords)
            route_lon = sum(c[1] for c in coords) / len(coords)
            pending_trees.sort(
                key=lambda t: haversine_m(t["_lat"], t["_lon"], route_lat, route_lon)
            )
            for t in pending_trees[:MAX_TREES]:
                objects.append({"polygon": t["polygon"], "height": t["height"], "type": "tree"})

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

def build_shadow_polys(
    buildings: list,
    sun_alt: float,
    sun_az: float,
    leaf_factor: float = 1.0,
) -> list:
    """
    Строит список полигонов теней (тень + контур здания) из списка объектов.
    Используется как в analyse_route, так и в find_shade_route.

    leaf_factor (0.15–1.0): сезонный коэффициент листвы деревьев.
    Применяется только к объектам типа «tree» и «forest»:
    - зимой деревья почти не дают тени (голые ветки → 0.15)
    - летом — полная листва → 1.0
    """
    polys: list[Polygon] = []
    if sun_alt > 3:
        for b in buildings:
            h = b["height"]
            if b.get("type") in ("tree", "forest"):
                h = h * leaf_factor          # масштабируем высоту тени деревьев
            sp = shadow_polygon(b["polygon"], h, sun_alt, sun_az)
            if sp is not None:
                polys.append(sp)
            polys.append(b["polygon"])
    return polys

# ─── Основной анализ маршрута (оптимизирован через STRtree) ──────────────────

def analyse_route(points, buildings, sun_alt, sun_az, weather,
                  walk_speed=1.35, depart_dt=None, leaf_factor=1.0):
    """
    Анализирует маршрут и возвращает метрики тени.

    shade_fraction вычисляется СОГЛАСОВАННО с Dijkstra: та же физика (SVF,
    H/W, ориентация улицы) + полигоны теней. Благодаря этому процент тени
    у маршрута «Теневой» реально отражает то, что он оптимизировал.

    Физические переменные:
      - Астрономические:  sun_alt, sun_az (из ephem) — пересчитываются каждые
                          SUN_RECALC_INTERVAL_S секунд ходьбы для длинных маршрутов
      - Геометрия:        H (высота зданий), W (ширина улицы), H/W ratio
      - SVF:              Sky View Factor = W / √(W²+H²)
      - shadow_len:       H / tan(sun_alt)   — длина тени
      - cross_shadow:     shadow_len × |sin(Δaz)|  — поперечная компонента
      - Атмосфера:        cloud_cover, uv_index, temp_c
    """
    if depart_dt is None:
        depart_dt = datetime.now(timezone.utc)

    # Текущая позиция солнца (будет обновляться по ходу маршрута)
    cur_sun_alt = sun_alt
    cur_sun_az  = sun_az
    last_sun_recalc_s = 0.0
    # Опорная точка для пересчёта: первая точка маршрута
    ref_lat = points[0][0] if points else 0.0
    ref_lon = points[0][1] if points else 0.0

    shadow_polys = build_shadow_polys(buildings, cur_sun_alt, cur_sun_az, leaf_factor=leaf_factor)
    poly_tree    = STRtree(shadow_polys) if shadow_polys else None

    # Пространственный индекс зданий — для физической модели тени
    bld_index_tuple, _, _, bld_heights = _build_building_index(buildings)

    cloud_factor = max(0, 1 - weather["cloud_cover"] / 100 * 0.7)
    uv_base      = weather["uv_index"] * cloud_factor

    shade_acc  = 0.0   # накопленный вес тени (непрерывный, не двоичный)
    uv_total   = 0.0
    heat_total = 0.0
    side_guidance: list[dict] = []
    last_side  = None
    dist_acc   = 0.0
    shade_seq: list[bool] = []   # для shade_map (пороговое значение 0.4)
    n_pts      = 0

    for i, (lat, lon) in enumerate(points):
        n_pts += 1

        # ── Динамический пересчёт позиции солнца каждые 5 мин ходьбы ────────
        elapsed_s = dist_acc / max(walk_speed, 0.1)
        if elapsed_s - last_sun_recalc_s >= SUN_RECALC_INTERVAL_S and i > 0:
            recalc_dt   = depart_dt + timedelta(seconds=elapsed_s)
            cur_sun_alt, cur_sun_az = sun_position(ref_lat, ref_lon, recalc_dt)
            last_sun_recalc_s = elapsed_s
            # Перестраиваем полигоны теней с новым положением солнца
            shadow_polys = build_shadow_polys(buildings, cur_sun_alt, cur_sun_az, leaf_factor=leaf_factor)
            poly_tree    = STRtree(shadow_polys) if shadow_polys else None
            log.debug(f"Sun recalc at {elapsed_s:.0f}s: alt={cur_sun_alt:.1f}° az={cur_sun_az:.1f}°")

        # ── Направление улицы в этой точке ──────────────────────────────────
        if i < len(points) - 1:
            seg_b = bearing_deg(lat, lon, points[i + 1][0], points[i + 1][1])
        elif i > 0:
            seg_b = bearing_deg(points[i - 1][0], points[i - 1][1], lat, lon)
        else:
            seg_b = 0.0

        # ── Метод 1: полигонная проверка (OSM-геометрия) ─────────────────────
        pt = Point(lon, lat)
        poly_shade = 0.0
        if poly_tree is not None:
            for idx in poly_tree.query(pt):
                if shadow_polys[idx].contains(pt):
                    poly_shade = 1.0
                    break

        # ── Метод 2: физика урбан-каньона (SVF + H/W + ориентация) ──────────
        phys_shade = _point_physics_shade(
            lat, lon, seg_b,
            bld_index_tuple, bld_heights,
            cur_sun_alt, cur_sun_az,
        ) if cur_sun_alt > 1.0 else 0.0

        # Итоговая тень: полигоны точны → полный вес; физика 90%
        shade_score = max(poly_shade, phys_shade * 0.90)
        shade_acc  += shade_score

        # Двоичный флаг для side_guidance и shade_map (порог 0.4)
        in_shade = shade_score >= 0.4
        shade_seq.append(in_shade)

        # УФ и тепловая нагрузка
        uv_total   += uv_base * (0.05 if in_shade else 1.0)
        heat_total += weather["temp_c"] * (0.6 if in_shade else 1.0)

        # ── Сторона улицы для подсказки ──────────────────────────────────────
        if i < len(points) - 1 and poly_tree is not None:
            ll = offset_point(lat, lon, SIDE_OFFSET_M, (seg_b - 90) % 360)
            rl = offset_point(lat, lon, SIDE_OFFSET_M, (seg_b + 90) % 360)
            lp = Point(ll[1], ll[0])
            rp = Point(rl[1], rl[0])
            left_sh  = any(shadow_polys[j].contains(lp) for j in poly_tree.query(lp))
            right_sh = any(shadow_polys[j].contains(rp) for j in poly_tree.query(rp))
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
            dist_acc += haversine_m(points[i - 1][0], points[i - 1][1], lat, lon)

    total_pts  = max(n_pts, 1)
    total_m    = dist_acc
    duration_s = total_m / walk_speed
    shade_frac = shade_acc / total_pts   # непрерывный 0–1
    is_night   = sun_alt <= 1.0 and cur_sun_alt <= 1.0  # солнце под горизонтом
    shade_min  = duration_s / 60 if is_night else duration_s * shade_frac / 60
    sun_min    = 0.0        if is_night else duration_s * (1 - shade_frac) / 60
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
        "is_night":       is_night,
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
    client: httpx.AsyncClient,
    overpass_url: str = "https://overpass-api.de/api/interpreter",
) -> list[tuple[float, float, float, float]]:
    """
    Загружает пешеходную сеть улиц в bbox (s, w, n, e) из Overpass.
    Возвращает список отрезков (lat1, lon1, lat2, lon2).

    Включает все проходимые типы: жилые улицы, тротуары, пешеходные зоны,
    парковые дорожки, сервисные проезды и т.д.
    Исключает: автомагистрали, строящиеся/предложенные/заброшенные дороги.
    """
    query = f"""[out:json][timeout:25];
way["highway"~"footway|path|pedestrian|living_street|residential|service|track|steps|unclassified|tertiary|secondary|primary"]
   ({s:.5f},{w:.5f},{n:.5f},{e:.5f});
out body geom;"""
    try:
        r = await client.post(
            overpass_url,
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
            poly = b["polygon"]
            raw_pt = poly.centroid
            # Для Г/П/U-образных зданий centroid может лежать вне полигона —
            # используем representative_point(), который гарантированно внутри
            c = raw_pt if poly.contains(raw_pt) else poly.representative_point()
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


# ─── Комплексная физика тени: SVF + урбан-каньон ─────────────────────────────
#
# Переменные модели:
#   Прямые астрономические:   sun_alt (высота солнца), sun_az (азимут солнца)
#   Геометрия застройки:      H (высота здания), W (ширина улицы), H/W ratio
#   Ориентация улицы:         seg_bearing (азимут оси), street_normal (перпендикуляр)
#   Угол падения лучей:       delta = |sun_az − street_normal|
#
#   Вычисляемые:
#     shadow_len = H / tan(sun_alt)           — длина тени от здания
#     cross_shadow = shadow_len × |sin(delta)| — поперечная компонента тени
#     SVF = W / √(W² + H²)                    — Sky View Factor (каньон)
#     shade_raw = cross_shadow / (W/2)         — покрытие тротуара тенью
#     shade = shade_raw + (1−SVF)×0.25        — с бонусом за глубокий каньон
#
# Два независимых метода, используемых совместно:
#   1. Полигоны теней (OSM-геометрия): точно, но только там где построены
#   2. Физика урбан-каньона (cKDTree): везде, масштабируется с ориентацией улицы
# Итог: максимум из обоих + непрерывная шкала 0.0–1.0

def _leaf_factor(month: int, lat: float = 0.0) -> float:
    """
    Сезонный коэффициент листвы деревьев (0.15 зима → 1.0 лето).

    Учитывает полушарие: в Южном (lat < 0) сезоны противоположны.
      - Северное (BA — НЕТ): пик в июле  (month 7)
      - Южное   (BA — ДА):  пик в январе (month 1)

    Диапазон: 0.15 (местная зима) … 1.0 (местное лето).
    """
    peak_month = 1 if lat < 0 else 7         # январь для южного полушария
    angle = 2 * math.pi * (month - peak_month) / 12
    raw   = (1.0 + math.cos(angle)) / 2.0
    return 0.15 + 0.85 * raw


def _sky_view_factor(H: float, W: float) -> float:
    """
    SVF — коэффициент видимости неба для симметричного городского каньона.

        SVF = W / √(W² + H²)

    SVF=1.0 → открытое небо (широкая улица, нет зданий)
    SVF=0.0 → закрытый каньон (узкая улица, очень высокие здания)

    Низкий SVF → больше диффузной тени даже когда прямые лучи не падают.
    """
    if H <= 0:
        return 1.0
    return W / math.sqrt(W ** 2 + H ** 2)


def _canyon_shade_fraction(
    H: float, W: float,
    seg_bearing: float,
    sun_az: float, sun_alt: float,
) -> float:
    """
    Доля тени на тротуаре из физики урбан-каньона (0.0–1.0).

    Физика:
      1. shadow_len = H / tan(sun_alt)          — длина тени от здания
      2. cross = shadow_len × |sin(sun_az − street_normal)|  — поперечная компонента
      3. shade_raw = cross / (W/2)              — покрытие (W/2 = расстояние до центра)
      4. SVF-бонус: (1−SVF)×0.25               — диффузная тень в глубоком каньоне

    Параметры:
      H: средняя высота ближайших зданий (м)
      W: ширина улицы curb-to-curb (м)
      seg_bearing: азимут оси улицы (°)
      sun_az: азимут солнца (°)
      sun_alt: высота солнца (°)
    """
    if sun_alt <= 1.0:
        return 0.95    # ночь / рассвет
    if H <= 0:
        return 0.0

    street_normal = (seg_bearing + 90) % 360
    delta = ((sun_az - street_normal + 360) % 360)
    if delta > 180:
        delta = 360 - delta  # delta ∈ [0°, 90°]

    shadow_len   = H / math.tan(math.radians(max(sun_alt, 3.0)))
    cross_shadow = shadow_len * abs(math.sin(math.radians(delta)))
    shade_raw    = cross_shadow / max(W / 2.0, 1.0)

    # SVF-бонус: глубокий каньон даёт дополнительную рассеянную тень
    svf   = _sky_view_factor(H, W)
    bonus = (1.0 - svf) * 0.25

    return min(shade_raw + bonus, 1.0)


def _shade_at_pt(
    lat: float, lon: float,
    shadow_polys: list,
    tree_idx: Optional[STRtree],
) -> float:
    """Возвращает 1.0 если точка внутри теневого полигона, иначе 0.0."""
    if tree_idx is None:
        return 0.0
    pt = Point(lon, lat)
    for idx in tree_idx.query(pt):
        if shadow_polys[idx].contains(pt):
            return 1.0
    return 0.0


def _point_physics_shade(
    lat: float, lon: float,
    seg_bearing: float,
    bld_index,
    bld_heights: np.ndarray,
    sun_alt: float, sun_az: float,
    search_r_m: float = 40.0,
) -> float:
    """
    Физическая оценка тени в конкретной точке (урбан-каньон + SVF).

    Ищет ближайшие здания через cKDTree (O(log N)).
    Возвращает 0.0–1.0 на основе H/W ratio и ориентации улицы.
    """
    if sun_alt <= 1.0:
        return 0.95

    # Нет данных о зданиях → типичная московская застройка
    if bld_index is None or len(bld_heights) == 0:
        return _canyon_shade_fraction(12.0, 14.0, seg_bearing, sun_az, sun_alt)

    kd, lat0, lon0, cos_lat = bld_index
    py = (lat - lat0) * 111_320
    px = (lon - lon0) * 111_320 * cos_lat
    idxs = kd.query_ball_point([py, px], r=search_r_m)
    if not idxs:
        # Нет зданий рядом — открытое пространство
        return 0.0

    h_arr = bld_heights[idxs]
    H = float(h_arr.mean())
    n = len(idxs)
    # Оценка ширины улицы: плотнее застройка → уже улица
    W = 7.0 if n >= 8 else (10.0 if n >= 4 else (13.0 if n >= 2 else 16.0))

    return _canyon_shade_fraction(H, W, seg_bearing, sun_az, sun_alt)


def _segment_shade_score(
    la1: float, lo1: float, la2: float, lo2: float,
    shadow_polys: list,
    poly_tree: Optional[STRtree],
    bld_index,
    bld_heights: np.ndarray,
    sun_alt: float, sun_az: float,
    n_samples: int = 3,
) -> float:
    """
    Унифицированная оценка тени для отрезка улицы (0.0–1.0).

    ИСПОЛЬЗУЕТСЯ ВЕЗДЕ: в Dijkstra (веса рёбер) И в analyse_route (shade_fraction).
    Это гарантирует согласованность — маршрут оптимизируется и измеряется
    одной и той же функцией.

    Алгоритм:
      Для n_samples равномерно расставленных точек вдоль отрезка:
        1. Полигонная проверка (OSM-геометрия): точно, но покрывает ~15-20% улиц
        2. Физика урбан-каньона (H/W, SVF, ориентация): везде, даёт градиент
        score = max(poly_shade, physics_shade × 0.9)
      Итог = среднее по точкам.

    n_samples=3 для Dijkstra (быстро), n_samples=5 для analyse_route (точнее).
    """
    if sun_alt <= 1.0:
        return 0.95

    seg_bear = bearing_deg(la1, lo1, la2, lo2)
    ts = [i / (n_samples - 1) for i in range(n_samples)] if n_samples > 1 else [0.5]
    total = 0.0

    for t in ts:
        lat = la1 + t * (la2 - la1)
        lon = lo1 + t * (lo2 - lo1)

        poly  = _shade_at_pt(lat, lon, shadow_polys, poly_tree) if shadow_polys else 0.0
        phys  = _point_physics_shade(
            lat, lon, seg_bear, bld_index, bld_heights, sun_alt, sun_az
        )
        # Полигоны более точны → полный вес; физика — 90%
        total += max(poly, phys * 0.90)

    return total / len(ts)


# Обратная совместимость: старый edge scorer → новая функция
def _edge_shade_score(
    la1: float, lo1: float, la2: float, lo2: float,
    shadow_polys: list,
    poly_tree: Optional[STRtree],
    bld_index,
    bld_heights: np.ndarray,
    sun_alt: float,
    sun_az: float,
) -> float:
    return _segment_shade_score(
        la1, lo1, la2, lo2,
        shadow_polys, poly_tree,
        bld_index, bld_heights,
        sun_alt, sun_az,
        n_samples=3,
    )


# ─── Безопасность улиц ────────────────────────────────────────────────────────

def _build_safety_index(danger_zones: list):
    """
    Строит (STRtree, polys_list, risk_levels) для O(log N) point-in-polygon тестов.

    danger_zones: [(coords_list, risk_level), ...]
      coords_list — список (lat, lon) замкнутого полигона
      risk_level  — 1..3 (1=осторожно, 2=опасно, 3=избегать)

    STRtree позволяет сначала быстро отфильтровать кандидатов по bbox,
    а уже потом делать точный contains — вместо перебора всех N полигонов
    для каждого из 175k сегментов (было: O(175k×N), стало: O(175k×log N)).
    """
    polys: list = []
    risks: list = []
    for coords, risk_level in danger_zones:
        if len(coords) < 3:
            continue
        try:
            poly = Polygon([(lo, la) for la, lo in coords])  # shapely: (lon, lat)
            if poly.is_valid and not poly.is_empty:
                polys.append(poly)
                risks.append(float(risk_level))
        except Exception:
            pass
    if not polys:
        return None, [], []
    return STRtree(polys), polys, risks


def _safety_for_seg(la1: float, lo1: float, la2: float, lo2: float,
                    safety_tree, safety_polys: list, safety_risks: list) -> float:
    """
    Возвращает коэффициент безопасности сегмента: 1.0 = безопасно, < 1.0 = риск.

    Использует STRtree для O(log N) фильтрации по bbox, затем точный contains.
    - risk_level=1 → score=0.50 (умеренный риск, промзона)
    - risk_level=2 → score=0.33 (высокий риск)
    - risk_level=3 → score=0.20 (очень высокий, неформальное поселение)
    """
    if safety_tree is None:
        return 1.0
    pt = Point((lo1 + lo2) / 2.0, (la1 + la2) / 2.0)
    for idx in safety_tree.query(pt):
        if safety_polys[idx].contains(pt):
            return 1.0 / (1.0 + safety_risks[idx])
    return 1.0


def find_shade_loop(
    origin_lat: float,
    origin_lon: float,
    target_dist_m: float,
    street_segs: list,
    shadow_polys: list,
    buildings: list,
    sun_alt: float,
    sun_az: float,
    sun_penalty: float = 20.0,
    max_routes: int = 2,
    danger_zones: Optional[list] = None,
) -> list[dict]:
    """
    Строит топ-N круговых маршрутов (петель) с максимальной долей тени.

    Алгоритм:
    1. Генерируем 8 кандидат-точек вокруг origin (N, NE, E, … NW)
       на расстоянии target_dist_m / 3 от origin.
    2. Для каждой: Dijkstra origin → waypoint → origin (тень-оптимизация).
    3. Сортируем все рабочие петли по доле тени (убывание).
    4. Фильтр разнообразия: 2-й и далее маршруты должны иметь
       ≥90° разницы по азимуту с уже выбранными.

    Возвращает список dict:
      [{"path": [(lat,lon),...], "dist": float, "shade_fraction": float}, ...]
    или [] если ничего не построено.
    """
    if not street_segs:
        return []

    poly_tree: Optional[STRtree] = STRtree(shadow_polys) if shadow_polys else None
    bld_index_tuple, _, _, bld_heights = _build_building_index(buildings)

    # Строим STRtree-индекс безопасности: O(log N) вместо O(N) на сегмент
    # _build_safety_index возвращает (STRtree, polys_list, risks_list)
    if danger_zones:
        safety_tree, safety_polys, safety_risks = _build_safety_index(danger_zones)
    else:
        safety_tree, safety_polys, safety_risks = None, [], []
    has_safety = safety_tree is not None

    # Штраф за опасный район: при risk=3 (score=0.25) сегмент обходится в
    # d_m × (1 + 40 × 0.75) = d_m × 31 → роутер активно их избегает.
    SAFETY_PENALTY = 40.0

    PREC = 5
    adj: dict[tuple, list] = {}
    node_coords: dict[tuple, tuple] = {}

    def _nid(lat: float, lon: float) -> tuple:
        return (round(lat, PREC), round(lon, PREC))

    # Минимальная длина сегмента — отсекаем микро-сегменты перекрёстков
    # (в Москве ~40-60% сегментов <3м). Это сохраняет связность графа.
    MIN_SEG_M = 3.0

    # ── Предвычисление сетки безопасности ─────────────────────────────────────
    # Вместо point-in-polygon для каждого из 50k сегментов строим грубую сетку
    # 100×100м: для каждого из N_zones полигонов проверяем ячейки в его bbox.
    # Lookup на сегмент — O(1) по индексу ячейки.
    # При bbox 0.04° ≈ 4400м и шаге 0.001° ≈ 111м → сетка 40×40 = 1600 ячеек.
    GRID_DEG = 0.001   # ~111м на экваторе
    safety_grid: Optional[np.ndarray] = None
    grid_s = grid_w = 0.0
    grid_nrow = grid_ncol = 0

    if has_safety and street_segs:
        # Определяем bbox по сегментам
        all_la = [c for seg in street_segs for c in (seg[0], seg[2])]
        all_lo = [c for seg in street_segs for c in (seg[1], seg[3])]
        grid_s, grid_n = min(all_la), max(all_la)
        grid_w, grid_e = min(all_lo), max(all_lo)
        grid_nrow = max(1, int((grid_n - grid_s) / GRID_DEG) + 1)
        grid_ncol = max(1, int((grid_e - grid_w) / GRID_DEG) + 1)
        safety_grid = np.ones((grid_nrow, grid_ncol), dtype=np.float32)

        for poly, risk in zip(safety_polys, safety_risks):
            score = float(1.0 / (1.0 + risk))
            bnd = poly.bounds  # (lon_min, lat_min, lon_max, lat_max)
            r0 = max(0, int((bnd[1] - grid_s) / GRID_DEG))
            r1 = min(grid_nrow - 1, int((bnd[3] - grid_s) / GRID_DEG))
            c0 = max(0, int((bnd[0] - grid_w) / GRID_DEG))
            c1 = min(grid_ncol - 1, int((bnd[2] - grid_w) / GRID_DEG))
            for r in range(r0, r1 + 1):
                cell_lat = grid_s + (r + 0.5) * GRID_DEG
                for c in range(c0, c1 + 1):
                    cell_lon = grid_w + (c + 0.5) * GRID_DEG
                    if poly.contains(Point(cell_lon, cell_lat)):
                        if score < safety_grid[r, c]:
                            safety_grid[r, c] = score

    def _grid_safety(mid_lat: float, mid_lon: float) -> float:
        """O(1) lookup в предвычисленной сетке безопасности."""
        if safety_grid is None:
            return 1.0
        r = min(grid_nrow - 1, max(0, int((mid_lat - grid_s) / GRID_DEG)))
        c = min(grid_ncol - 1, max(0, int((mid_lon - grid_w) / GRID_DEG)))
        return float(safety_grid[r, c])

    # ── Строим граф ────────────────────────────────────────────────────────────
    for la1, lo1, la2, lo2 in street_segs:
        d_m = haversine_m(la1, lo1, la2, lo2)
        if d_m < MIN_SEG_M:
            continue
        n1, n2 = _nid(la1, lo1), _nid(la2, lo2)
        node_coords[n1] = (la1, lo1)
        node_coords[n2] = (la2, lo2)
        shade  = _edge_shade_score(la1, lo1, la2, lo2, shadow_polys, poly_tree,
                                   bld_index_tuple, bld_heights, sun_alt, sun_az)
        safety = _grid_safety((la1 + la2) / 2, (lo1 + lo2) / 2)
        # Полностью безопасный теневой маршрут: cost = d_m × 1.0
        # Опасный солнечный: cost = d_m × (1 + sun_penalty + safety_penalty)
        cost = d_m * (1.0 + sun_penalty * (1.0 - shade) + SAFETY_PENALTY * (1.0 - safety))
        adj.setdefault(n1, []).append((n2, d_m, cost))
        adj.setdefault(n2, []).append((n1, d_m, cost))

    if not adj:
        return []

    all_nodes = list(adj.keys())
    node_arr  = np.array([(node_coords[n][0], node_coords[n][1]) for n in all_nodes])

    def _nearest(lat: float, lon: float) -> tuple:
        diffs = node_arr - np.array([lat, lon])
        idx   = int(np.argmin((diffs ** 2).sum(axis=1)))
        return all_nodes[idx]

    def _dijkstra(src: tuple, dst: tuple, budget_m: float, deadline: float):
        INF = float("inf")
        g_cost:  dict = {src: 0.0}
        g_dist:  dict = {src: 0.0}
        g_shade: dict = {src: 0.0}
        came:    dict = {src: None}
        ctr = itertools.count()
        pq  = [(0.0, next(ctr), src)]
        while pq:
            if time.monotonic() > deadline:
                break
            c, _, node = heappop(pq)
            if c > g_cost.get(node, INF) + 1e-9:
                continue
            if node == dst:
                break
            for nb, ed, ec in adj.get(node, []):
                nd = g_dist[node] + ed
                if nd > budget_m:
                    continue
                nc = c + ec
                if nc < g_cost.get(nb, INF):
                    g_cost[nb]  = nc
                    g_dist[nb]  = nd
                    sf = 1.0 - (ec - ed) / max(ed * sun_penalty, 1e-6)
                    g_shade[nb] = g_shade[node] + ed * max(sf, 0.0)
                    came[nb]    = node
                    heappush(pq, (nc, next(ctr), nb))
        if dst not in came:
            return None
        path, n = [], dst
        while n is not None:
            path.append(node_coords[n])
            n = came[n]
        path.reverse()
        return path, g_dist.get(dst, 0.0), g_shade.get(dst, 0.0)

    start    = _nearest(origin_lat, origin_lon)
    deadline = time.monotonic() + 25.0
    # Бюджет на каждый отрезок петли (origin→wp и wp→origin).
    # 1.2× target_dist даёт Dijkstra достаточно свободы для разных планировок улиц.
    # Ограничиваем 8000м чтобы длинные прогулки (300 мин, bbox 4.4 км) не
    # исследовали весь граф с огромным бюджетом — иначе ~60с вместо 20с.
    half_budget = min(target_dist_m * 1.2, 8_000.0)

    # 8 кандидат-точек на расстоянии ~1/3 target вокруг origin
    wp_dist = target_dist_m / 3.0
    candidates = []
    for bearing in range(0, 360, 45):
        wp_lat, wp_lon = offset_point(origin_lat, origin_lon, wp_dist, bearing)
        wp_node = _nearest(wp_lat, wp_lon)
        if wp_node != start:
            candidates.append((bearing, wp_node))

    all_loops: list[dict] = []

    for bearing, wp in candidates:
        if time.monotonic() > deadline:
            break
        r1 = _dijkstra(start, wp, half_budget, deadline)
        if r1 is None:
            continue
        path1, d1, shd1 = r1
        r2 = _dijkstra(wp, start, half_budget, deadline)
        if r2 is None:
            continue
        path2, d2, shd2 = r2
        full_path   = path1 + path2[1:]
        total_dist  = d1 + d2
        total_shade = shd1 + shd2
        sf = total_shade / max(total_dist, 1.0)

        # Вычисляем safety_score: доля маршрута (по дистанции) в безопасных зонах
        # Используем ту же сетку — O(1) на сегмент
        if has_safety:
            safe_dist = 0.0
            for i in range(len(full_path) - 1):
                la1_, lo1_ = full_path[i]
                la2_, lo2_ = full_path[i + 1]
                seg_d = haversine_m(la1_, lo1_, la2_, lo2_)
                safe_dist += seg_d * _grid_safety((la1_ + la2_) / 2, (lo1_ + lo2_) / 2)
            route_safety = safe_dist / max(total_dist, 1.0)
        else:
            route_safety = 1.0

        all_loops.append({
            "bearing":        bearing,
            "path":           full_path,
            "dist":           total_dist,
            "shade_fraction": sf,
            "safety_score":   round(route_safety, 3),
        })
        log.info(
            f"Loop candidate bearing={bearing}: dist={int(total_dist)}m "
            f"shade={sf:.1%} safety={route_safety:.1%}"
        )

    if not all_loops:
        log.warning("find_shade_loop: no loop found")
        return []

    # Сортируем по доле тени (лучшие первыми)
    all_loops.sort(key=lambda x: x["shade_fraction"], reverse=True)

    # Фильтр разнообразия: ≥90° разницы по азимуту между выбранными маршрутами
    result: list[dict] = []
    chosen_bearings: list[float] = []
    for loop in all_loops:
        if not chosen_bearings:
            result.append(loop)
            chosen_bearings.append(loop["bearing"])
        else:
            # Для каждого уже выбранного маршрута вычисляем симметричный угол
            # (min из двух направлений), затем берём минимум по всем
            min_diff = min(
                min(abs(loop["bearing"] - b) % 360,
                    360 - abs(loop["bearing"] - b) % 360)
                for b in chosen_bearings
            )
            if min_diff >= 90:
                result.append(loop)
                chosen_bearings.append(loop["bearing"])
        if len(result) >= max_routes:
            break

    # Если разнообразных маршрутов не хватило, добираем без фильтра
    if len(result) < max_routes and len(all_loops) > len(result):
        for loop in all_loops:
            if loop not in result:
                result.append(loop)
            if len(result) >= max_routes:
                break

    for i, r in enumerate(result):
        log.info(f"Result route #{i+1}: bearing={r['bearing']} "
                 f"dist={int(r['dist'])}m shade={r['shade_fraction']:.1%}")

    return result


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
            # Выбираем кандидата ближе к теневой стороне от солнца.
            # Используем _point_physics_shade (физическая оценка тени в точке).
            best_wp = max(
                wp_candidates,
                key=lambda wp: _shade_at_pt(wp[0], wp[1], shadow_polys, poly_tree)
                               + _point_physics_shade(
                                   wp[0], wp[1],
                                   direct_bear,          # ориентация улицы ≈ прямое направление
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
