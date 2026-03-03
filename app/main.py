"""
Safe Shadow Backend — три маршрута по физике тени.

Всегда возвращает ровно 3 маршрута:
  route-fast     : Самый быстрый  — кратчайший маршрут от OSRM
  route-balanced : Оптимальный    — лучший баланс время/тень из OSRM-альтернатив
  route-shade    : Максимум тени  — строится через граф улиц OSM
                   с весами по физике тени (солнечная геометрия + геометрия застройки)

Физика маршрута route-shade учитывает:
  - Временные/астрономические параметры: дата, время, широта, долгота → ephem
  - Высота и азимут солнца, солнечная деклинация, часовой угол
  - Геометрия застройки: H/W ratio, ориентация улицы, угол закрытия небосвода
  - Локальные затеняющие объекты: здания, деревья, лесные массивы (Overpass)
  - Sky View Factor (через shadow_reach / street_width)
  - Коэффициент рассеяния УФ (облачность, УФ-индекс)
"""
import asyncio
import math
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from typing import Literal, Optional, List

import httpx
import polyline as polyline_lib
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .shadow import (
    sun_position, interpolate_route, bearing_deg, offset_point,
    fetch_buildings, fetch_weather, analyse_route,
    build_shadow_polys, fetch_street_network, find_shade_route,
    haversine_m, _leaf_factor,
    _build_building_index, _point_physics_shade,
)
from .ratelimit import RateLimiter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

APP_VERSION = "0.5.0"
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))

app = FastAPI(title="Safe Shadow Backend", version=APP_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
rl = RateLimiter(per_minute=RATE_LIMIT_PER_MIN)

_executor = ThreadPoolExecutor(max_workers=4)

OSRM_BASE_FOOT = "https://routing.openstreetmap.de/routed-foot"
OSRM_BASE_BIKE = "https://routing.openstreetmap.de/routed-bike"

# ─── Кэш зданий (TTL 10 минут) ────────────────────────────────────────────────
_buildings_cache: dict = {}
_BUILDINGS_CACHE_TTL = 600

# ─── Кэш сети улиц (TTL 30 минут) ─────────────────────────────────────────────
_streets_cache: dict = {}
_STREETS_CACHE_TTL = 1800  # 30 минут

def _cache_key(coords: list) -> str:
    if not coords:
        return ""
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return f"{min(lats):.2f},{min(lons):.2f},{max(lats):.2f},{max(lons):.2f}"

async def _fetch_buildings_cached(coords: list, client) -> list:
    key = _cache_key(coords)
    now = time.time()
    if key in _buildings_cache:
        buildings, ts = _buildings_cache[key]
        if now - ts < _BUILDINGS_CACHE_TTL:
            log.info(f"Buildings cache HIT ({len(buildings)} buildings)")
            return buildings
    try:
        buildings = await asyncio.wait_for(
            fetch_buildings(coords, client), timeout=30.0
        )
        _buildings_cache[key] = (buildings, now)
        if len(_buildings_cache) > 50:
            oldest = min(_buildings_cache, key=lambda k: _buildings_cache[k][1])
            del _buildings_cache[oldest]
        return buildings
    except asyncio.TimeoutError:
        log.warning("Overpass timeout (30s) — без теней")
        return []

async def _fetch_streets_cached(
    bbox: tuple[float, float, float, float],
    client: httpx.AsyncClient
) -> list:
    """Возвращает сеть улиц из кэша (TTL 30 мин) или из Overpass API."""
    s, w, n, e = bbox
    key = f"{s:.3f},{w:.3f},{n:.3f},{e:.3f}"
    now = time.time()
    if key in _streets_cache:
        segs, ts = _streets_cache[key]
        if now - ts < _STREETS_CACHE_TTL:
            log.info(f"Streets cache HIT ({len(segs)} segments)")
            return segs
    try:
        segs = await asyncio.wait_for(
            fetch_street_network(s, w, n, e, client), timeout=28.0
        )
        _streets_cache[key] = (segs, now)
        if len(_streets_cache) > 20:   # не даём кэшу разрастись
            oldest = min(_streets_cache, key=lambda k: _streets_cache[k][1])
            del _streets_cache[oldest]
        return segs
    except asyncio.TimeoutError:
        log.warning("Streets fetch timeout — пустой граф")
        return []

# ─── Модели ───────────────────────────────────────────────────────────────────

class LatLon(BaseModel):
    lat: float
    lon: float
    source: str = "search"

class ClientInfo(BaseModel):
    platform: str = "android"
    app_version: str = APP_VERSION
    device_id: str = Field(default="unknown", min_length=1)

class RoutesRequest(BaseModel):
    # preset сохранён для обратной совместимости, но игнорируется —
    # сервер всегда возвращает 3 маршрута (fast / balanced / shade).
    preset: Optional[Literal["fast", "less_uv", "cooler"]] = None
    origin: LatLon
    destination: LatLon
    departure_time: Optional[str] = None
    walk_speed_mps: float = Field(1.35, ge=0.5, le=20.0)
    transport: Literal["foot", "bike"] = "foot"
    client: ClientInfo = ClientInfo()

class Metrics(BaseModel):
    temp_feels_avg_c: float
    sun_minutes: float
    shade_minutes: float
    uv_dose: float
    heat_load: float
    confidence: Literal["high", "medium", "low"]

class SideGuidanceItem(BaseModel):
    from_m: Optional[int] = None
    to_m: Optional[int] = None
    at_m: Optional[int] = None
    preferred_side: Optional[Literal["left", "right"]] = None
    action: Optional[Literal["switch_side"]] = None
    note: Optional[str] = None

class RouteOut(BaseModel):
    id: str
    label: str        # «Быстрый» / «Оптимальный» / «Теневой»
    polyline: str
    distance_m: int
    duration_s: int
    metrics: Metrics
    side_guidance: List[SideGuidanceItem]
    shade_map: str = ""  # "0110..." 1=тень 0=солнце, шаг=40м

class RoutesResponse(BaseModel):
    routes: List[RouteOut]

# ─── OSRM ─────────────────────────────────────────────────────────────────────

async def _osrm_single(
    waypoints: str,
    client: httpx.AsyncClient,
    alternatives: bool = True,
    transport: str = "foot"
) -> list[dict]:
    osrm_base = OSRM_BASE_BIKE if transport == "bike" else OSRM_BASE_FOOT
    profile   = "bike" if transport == "bike" else "foot"
    url = (
        f"{osrm_base}/route/v1/{profile}/{waypoints}"
        f"?alternatives={'true' if alternatives else 'false'}"
        f"&overview=full&geometries=polyline"
    )
    try:
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        result = []
        for route in r.json().get("routes", [])[:3]:
            enc = route.get("geometry", "")
            result.append({
                "polyline": enc,
                "coords":   polyline_lib.decode(enc),
                "distance": route["distance"],
                "duration": route["duration"],
            })
        return result
    except Exception:
        return []


def _offset_coord(lat: float, lon: float, dx_m: float, dy_m: float):
    dlat = dy_m / 111_320
    dlon = dx_m / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


async def _osrm_routes(
    origin: LatLon,
    dest: LatLon,
    client: httpx.AsyncClient,
    transport: str = "foot"
) -> list[dict]:
    """Возвращает до 5 маршрутов: прямые + 8 детуров для выбора balanced-маршрута."""
    o = f"{origin.lon},{origin.lat}"
    d = f"{dest.lon},{dest.lat}"

    direct = await _osrm_single(f"{o};{d}", client, alternatives=True, transport=transport)
    if not direct:
        raise HTTPException(502, "Не удалось получить маршрут от OSRM")

    seen_polylines = {r["polyline"] for r in direct}
    all_routes = list(direct)

    if len(all_routes) < 4:
        mid_lat = (origin.lat + dest.lat) / 2
        mid_lon = (origin.lon + dest.lon) / 2
        dist_m  = direct[0]["distance"]
        offset  = min(max(dist_m * 0.15, 80), 250)
        d45     = offset * 0.707

        detour_offsets = [
            (offset, 0), (-offset, 0), (0, offset), (0, -offset),
            (d45, d45), (-d45, d45), (d45, -d45), (-d45, -d45),
        ]
        tasks = []
        for dx, dy in detour_offsets:
            wlat, wlon = _offset_coord(mid_lat, mid_lon, dx, dy)
            wp = f"{o};{wlon},{wlat};{d}"
            tasks.append(_osrm_single(wp, client, alternatives=False, transport=transport))

        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, list):
                for r in res:
                    if r["polyline"] not in seen_polylines and \
                       r["distance"] <= direct[0]["distance"] * 1.7:
                        seen_polylines.add(r["polyline"])
                        all_routes.append(r)
                        if len(all_routes) >= 5:
                            break
            if len(all_routes) >= 5:
                break

    all_routes.sort(key=lambda r: r["distance"])
    return all_routes[:5]

# ─── Вспомогательные ──────────────────────────────────────────────────────────

def _parse_depart(departure_time: Optional[str]) -> datetime:
    if departure_time:
        try:
            return datetime.fromisoformat(departure_time.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


async def _analyse_batch(
    raw_routes: list[dict],
    buildings:  list,
    sun_alt:    float,
    sun_az:     float,
    weather:    dict,
    walk_speed: float,
    depart_dt:  datetime,
    loop:       asyncio.AbstractEventLoop
) -> list[tuple[dict, dict]]:
    """Анализирует сырые маршруты, возвращает [(raw, stats), ...]."""
    result = []
    for raw in raw_routes:
        pts   = interpolate_route(raw["coords"])
        stats = await loop.run_in_executor(
            _executor,
            partial(analyse_route,
                points     = pts,
                buildings  = buildings,
                sun_alt    = sun_alt,
                sun_az     = sun_az,
                weather    = weather,
                walk_speed = walk_speed,
                depart_dt  = depart_dt,
            )
        )
        result.append((raw, stats))
    return result


def _make_route_out(route_id: str, label: str, raw: dict, stats: dict) -> RouteOut:
    return RouteOut(
        id            = route_id,
        label         = label,
        polyline      = raw["polyline"],
        distance_m    = stats["distance_m"],
        duration_s    = stats["duration_s"],
        metrics       = Metrics(
            temp_feels_avg_c = stats["temp_feels_c"],
            sun_minutes      = stats["sun_min"],
            shade_minutes    = stats["shade_min"],
            uv_dose          = stats["uv_dose"],
            heat_load        = stats["heat_load"],
            confidence       = stats["confidence"],
        ),
        side_guidance = [SideGuidanceItem(**g) for g in stats["side_guidance"]],
        shade_map     = stats.get("shade_map", ""),
    )

# ─── Вычисление bbox для сети улиц ────────────────────────────────────────────

def _streets_bbox(
    origin: LatLon, dest: LatLon
) -> tuple[float, float, float, float]:
    """
    Bbox для Overpass-запроса сети улиц.
    Допускает детуры до 50% длины прямого пути в каждую сторону,
    минимум 400 м, максимум 1500 м от крайних точек.
    """
    direct_m = haversine_m(origin.lat, origin.lon, dest.lat, dest.lon)
    # Bbox: достаточный для max_detour=4.0 — но не слишком большой для скорости.
    # pad = 60% от прямого расстояния, но не меньше 400м и не больше 1200м.
    pad_m    = min(max(direct_m * 0.6, 400), 1200)
    mid_lat  = (origin.lat + dest.lat) / 2
    pad_lat  = pad_m / 111_320
    pad_lon  = pad_m / (111_320 * math.cos(math.radians(mid_lat)))

    s = min(origin.lat, dest.lat) - pad_lat
    n = max(origin.lat, dest.lat) + pad_lat
    w = min(origin.lon, dest.lon) - pad_lon
    e = max(origin.lon, dest.lon) + pad_lon
    return s, w, n, e

# ─── Endpoint ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.post("/debug-shade")
async def debug_shade(req: dict):
    """Debug endpoint: returns intermediate shade routing info."""
    import math as _math

    olat = req["olat"]; olon = req["olon"]
    dlat = req["dlat"]; dlon = req["dlon"]
    dt_str = req.get("departure_time", datetime.now(timezone.utc).isoformat())
    depart_dt = _parse_depart(dt_str)
    clat = (olat + dlat) / 2; clon = (olon + dlon) / 2
    sun_alt, sun_az = sun_position(clat, clon, depart_dt)
    direct_m = haversine_m(olat, olon, dlat, dlon)

    # Streets bbox
    pad_m = min(max(direct_m * 0.6, 400), 1200)
    mid_lat = clat; cos_lat = _math.cos(_math.radians(mid_lat))
    pad_lat = pad_m / 111_320; pad_lon = pad_m / (111_320 * cos_lat)
    s = min(olat, dlat) - pad_lat; n = max(olat, dlat) + pad_lat
    w = min(olon, dlon) - pad_lon; e = max(olon, dlon) + pad_lon

    import httpx
    async with httpx.AsyncClient() as client:
        buildings = await _fetch_buildings_cached([(olat, olon), (dlat, dlon)], client)
        street_segs = await asyncio.wait_for(
            fetch_street_network(s, w, n, e, client), timeout=25.0
        )
    leaf = _leaf_factor(depart_dt.month)
    shadow_polys = build_shadow_polys(buildings, sun_alt, sun_az, leaf_factor=leaf)

    loop = asyncio.get_event_loop()
    shade_result = await loop.run_in_executor(
        _executor,
        partial(find_shade_route, olat, olon, dlat, dlon,
                street_segs, shadow_polys, buildings, sun_alt, sun_az, 1.5, 20.0)
    )

    return {
        "sun_alt": round(sun_alt, 2), "sun_az": round(sun_az, 2),
        "direct_m": round(direct_m, 1),
        "buildings": len(buildings), "street_segs": len(street_segs),
        "shadow_polys": len(shadow_polys),
        "shade_route": {
            "found": shade_result is not None,
            "dist_m": round(shade_result[1], 1) if shade_result else None,
            "points": len(shade_result[0]) if shade_result else 0,
        } if shade_result is not None else {"found": False}
    }


@app.post("/best-time")
async def best_time(req: dict):
    """
    Быстрый расчёт лучшего времени выхода (без OSRM и Dijkstra).

    Вход:  { olat, olon, dlat, dlon }
    Выход: { slots: [{hour, shade_score}], best_hour, best_shade }

    Работает за 2–4 с вместо 2 минут у старого метода (12× fetchRoutes).
    Алгоритм: 10 точек вдоль прямой линии → физика урбан-каньона → среднее.
    Кэш зданий 10 мин делает повторные запросы мгновенными.
    """
    olat = float(req["olat"]); olon = float(req["olon"])
    dlat = float(req["dlat"]); dlon = float(req["dlon"])

    # 10 равномерных точек вдоль прямой (достаточно для оценки тени)
    n_pts = 10
    sample_pts = [
        (olat + i * (dlat - olat) / (n_pts - 1),
         olon + i * (dlon - olon) / (n_pts - 1))
        for i in range(n_pts)
    ]

    async with httpx.AsyncClient() as client:
        buildings = await _fetch_buildings_cached(sample_pts, client)

    bld_index_tuple, _, _, bld_heights = _build_building_index(buildings)
    seg_bear = bearing_deg(olat, olon, dlat, dlon)
    clat = (olat + dlat) / 2
    clon = (olon + dlon) / 2

    # Используем сегодняшнюю дату (только время меняется)
    today = datetime.now(timezone.utc).date()

    slots = []
    best_hour  = 6
    best_shade = 0.0

    for hour in range(6, 22):
        dt = datetime(today.year, today.month, today.day, hour, 0, 0, tzinfo=timezone.utc)
        sun_alt, sun_az = sun_position(clat, clon, dt)

        if sun_alt <= 0.0:
            # Ночью / до рассвета — солнца нет, но листовой маршрут не нужен
            shade_score = 0.0
        else:
            scores = [
                _point_physics_shade(lat, lon, seg_bear,
                                     bld_index_tuple, bld_heights,
                                     sun_alt, sun_az)
                for lat, lon in sample_pts
            ]
            shade_score = float(sum(scores) / len(scores))

        slots.append({"hour": hour, "shade_score": round(shade_score, 3)})

        if shade_score > best_shade:
            best_shade = shade_score
            best_hour  = hour

    log.info(f"/best-time: best={best_hour}:00 shade={best_shade:.1%}")
    return {
        "slots":      slots,
        "best_hour":  best_hour,
        "best_shade": round(best_shade, 3),
    }


@app.post("/routes", response_model=RoutesResponse)
async def routes(req: RoutesRequest):
    if not rl.allow(req.client.device_id):
        raise HTTPException(429, "RATE_LIMIT")

    depart_dt = _parse_depart(req.departure_time)
    clat = (req.origin.lat + req.destination.lat) / 2
    clon = (req.origin.lon + req.destination.lon) / 2

    # ── 1. Параллельно: OSRM маршруты + погода ───────────────────────────────
    async with httpx.AsyncClient() as client:
        raw_routes, weather = await asyncio.gather(
            _osrm_routes(req.origin, req.destination, client, transport=req.transport),
            fetch_weather(clat, clon, client)
        )

    sun_alt, sun_az = sun_position(clat, clon, depart_dt)
    log.info(f"Sun: alt={sun_alt:.1f}° az={sun_az:.1f}° | routes={len(raw_routes)}")

    # ── 2. Параллельно: здания + сеть улиц (разные запросы) ──────────────────
    all_coords  = [c for r in raw_routes for c in r["coords"]]
    streets_bbox = _streets_bbox(req.origin, req.destination)

    async with httpx.AsyncClient() as client:
        buildings, street_segs = await asyncio.gather(
            _fetch_buildings_cached(all_coords, client),
            _fetch_streets_cached(streets_bbox, client),
        )
    log.info(f"Buildings: {len(buildings)} | Street segs: {len(street_segs)}")

    # Сезонный коэффициент листвы деревьев (0.15 зима → 1.0 лето)
    leaf = _leaf_factor(depart_dt.month)

    # Строим теневые полигоны ОДИН РАЗ — используются везде
    shadow_polys = build_shadow_polys(buildings, sun_alt, sun_az, leaf_factor=leaf)

    loop = asyncio.get_event_loop()

    # ── 3. Анализ OSRM маршрутов ─────────────────────────────────────────────
    analyzed = await _analyse_batch(
        raw_routes, buildings, sun_alt, sun_az, weather,
        req.walk_speed_mps, depart_dt, loop
    )
    log.info(f"Analysed {len(analyzed)} OSRM routes")

    # ── 4. Графовый маршрут с максимальной тенью (в thread executor) ─────────
    # sun_penalty=5.0 → солнечный сегмент в 6× дороже теневого (агрессивно ищет тень)
    # max_detour=4.0 → маршрут может быть до 4× длиннее прямого расстояния
    # Велосипед: снижаем sun_penalty — скорость важнее, тень менее критична
    sun_penalty = 8.0 if req.transport == "bike" else 20.0

    shade_graph_result = await loop.run_in_executor(
        _executor,
        partial(
            find_shade_route,
            req.origin.lat, req.origin.lon,
            req.destination.lat, req.destination.lon,
            street_segs,
            shadow_polys,
            buildings,
            sun_alt,
            sun_az,
            1.5,           # max_detour: не длиннее 1.5× быстрого маршрута
            sun_penalty,   # 20.0 пешком, 8.0 велосипед
        )
    )

    # ── 5. Анализируем графовый маршрут (если удалось построить) ─────────────
    shade_graph_analyzed: Optional[tuple[dict, dict]] = None
    if shade_graph_result is not None:
        shade_path, shade_dist_m = shade_graph_result
        # Создаём «raw» объект в том же формате что и OSRM-маршруты
        shade_raw = {
            "polyline": polyline_lib.encode(shade_path),
            "coords":   shade_path,
            "distance": shade_dist_m,
            "duration": shade_dist_m / req.walk_speed_mps,
        }
        pts = interpolate_route(shade_path)
        shade_stats = await loop.run_in_executor(
            _executor,
            partial(analyse_route,
                points     = pts,
                buildings  = buildings,
                sun_alt    = sun_alt,
                sun_az     = sun_az,
                weather    = weather,
                walk_speed = req.walk_speed_mps,
                depart_dt  = depart_dt,
            )
        )
        shade_graph_analyzed = (shade_raw, shade_stats)
        log.info(
            f"✅ Shade graph route (OSM Dijkstra): {int(shade_dist_m)}m, "
            f"shade={shade_stats['shade_fraction']:.1%}, "
            f"nodes={len(shade_path)}"
        )
    else:
        log.info("⚠️ Shade graph routing returned None — using OSRM fallback")

    # ── 6. Выбираем 3 маршрута ────────────────────────────────────────────────
    #
    # Маршрут 1 (route-fast): кратчайший по времени из OSRM
    # Маршрут 2 (route-balanced): лучший баланс тень/время из OSRM
    # Маршрут 3 (route-shade): графовый маршрут (или fallback — макс. тени OSRM)

    # ── Маршрут 1: Быстрый ────────────────────────────────────────────────────
    fast_raw, fast_stats = min(analyzed, key=lambda x: x[0]["duration"])
    route_fast = _make_route_out("route-fast", "Быстрый", fast_raw, fast_stats)

    # ── Маршрут 2: Оптимальный (время + тень) ─────────────────────────────────
    # Нормализуем duration и sun_minutes → [0,1], минимизируем взвешенную сумму
    min_dur = min(r["duration"] for r, _ in analyzed)
    max_dur = max(r["duration"] for r, _ in analyzed) or 1
    max_sun = max(s["sun_min"] for _, s in analyzed) or 1

    def _balanced_score(raw: dict, stats: dict) -> float:
        dur_norm = (raw["duration"] - min_dur) / (max_dur - min_dur + 1)
        sun_norm = stats["sun_min"] / max_sun
        return 0.45 * dur_norm + 0.55 * sun_norm

    balanced_raw, balanced_stats = min(analyzed, key=lambda x: _balanced_score(*x))
    route_balanced = _make_route_out(
        "route-balanced", "Оптимальный", balanced_raw, balanced_stats
    )

    # ── Маршрут 3: Максимум тени ──────────────────────────────────────────────
    if shade_graph_analyzed is not None:
        shade_raw2, shade_stats2 = shade_graph_analyzed
        route_shade = _make_route_out("route-shade", "Теневой", shade_raw2, shade_stats2)
    else:
        # Fallback: самый теневой из OSRM-альтернатив
        shadow_raw, shadow_stats = max(
            analyzed, key=lambda x: x[1]["shade_fraction"]
        )
        route_shade = _make_route_out("route-shade", "Теневой", shadow_raw, shadow_stats)

    log.info(
        f"Routes: fast={route_fast.duration_s}s shade={route_fast.metrics.shade_minutes:.1f}min | "
        f"balanced={route_balanced.duration_s}s shade={route_balanced.metrics.shade_minutes:.1f}min | "
        f"shade={route_shade.duration_s}s shade={route_shade.metrics.shade_minutes:.1f}min"
    )

    return RoutesResponse(routes=[route_fast, route_balanced, route_shade])
