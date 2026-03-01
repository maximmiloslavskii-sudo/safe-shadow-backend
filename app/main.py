"""
Safe Shadow Backend — реальный алгоритм теневых маршрутов.
Поддерживает любой город мира.

Алгоритм (2 прохода):
  1. Получаем начальные маршруты (OSRM прямые + 8 детуров по сторонам света)
  2. Анализируем тень для каждого
  3. Если лучший маршрут даёт < 50% тени → ищем «солнечные горячие точки»
     и делаем целевые боковые детуры вокруг них
  4. Анализируем новые кандидаты, объединяем, сортируем
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
    fetch_buildings, fetch_weather, analyse_route
)
from .ratelimit import RateLimiter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

APP_VERSION = "0.3.0"
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))

app = FastAPI(title="Safe Shadow Backend", version=APP_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
rl = RateLimiter(per_minute=RATE_LIMIT_PER_MIN)

_executor = ThreadPoolExecutor(max_workers=4)

OSRM_BASE_FOOT = "https://routing.openstreetmap.de/routed-foot"
OSRM_BASE_BIKE = "https://routing.openstreetmap.de/routed-bike"

# ─── Кэш зданий (TTL 10 минут) ────────────────────────────────────────────────
# Ключ: rounded bbox string, значение: (buildings, timestamp)
_buildings_cache: dict = {}
_BUILDINGS_CACHE_TTL = 600  # 10 минут в секундах

def _cache_key(coords: list) -> str:
    """Округлённый bbox маршрута до ~1 км."""
    if not coords:
        return ""
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    # Округляем до 0.01° (~1.1 км)
    return f"{min(lats):.2f},{min(lons):.2f},{max(lats):.2f},{max(lons):.2f}"

async def _fetch_buildings_cached(coords: list, client) -> list:
    """Возвращает здания из кэша или запрашивает Overpass."""
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
        # Чистим старые записи кэша (держим не более 50)
        if len(_buildings_cache) > 50:
            oldest = min(_buildings_cache, key=lambda k: _buildings_cache[k][1])
            del _buildings_cache[oldest]
        return buildings
    except asyncio.TimeoutError:
        log.warning("Overpass timeout (30s) — без теней")
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
    preset: Literal["fast", "less_uv", "cooler"]
    origin: LatLon
    destination: LatLon
    departure_time: Optional[str] = None
    walk_speed_mps: float = Field(1.35, ge=0.5, le=20.0)   # велосипед до 20 м/с
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
    polyline: str
    distance_m: int
    duration_s: int
    metrics: Metrics
    side_guidance: List[SideGuidanceItem]
    shade_map: str = ""   # "0110..." 1=shade, 0=sun, шаг=40м

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
    """Возвращает до 5 маршрутов (пешком или велосипед): прямые + 8 детуров."""
    o = f"{origin.lon},{origin.lat}"
    d = f"{dest.lon},{dest.lat}"

    direct = await _osrm_single(f"{o};{d}", client, alternatives=True, transport=transport)
    if not direct:
        raise HTTPException(502, "Не удалось получить маршрут от OSRM")

    seen_polylines = {r["polyline"] for r in direct}
    all_routes = list(direct)

    if len(all_routes) < 5:
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

# ─── Shade-seeking helpers ────────────────────────────────────────────────────

def _sun_hotspots(
    interp_points: list[tuple[float, float]],
    shade_map: str,
    min_run_pts: int = 3,   # ≥3×40м = ≥120м сплошного солнца
    max_hotspots: int = 2
) -> list[dict]:
    """
    Находит самые длинные «солнечные» участки маршрута и возвращает их
    географические центры с азимутом движения.
    """
    hotspots = []
    i = 0
    while i < len(shade_map):
        if shade_map[i] == '0':
            j = i
            while j < len(shade_map) and shade_map[j] == '0':
                j += 1
            run_len = j - i
            if run_len >= min_run_pts:
                mid    = i + run_len // 2
                pt_idx = min(mid, len(interp_points) - 1)
                lat, lon = interp_points[pt_idx]
                prev_i = max(0, pt_idx - 1)
                next_i = min(len(interp_points) - 1, pt_idx + 1)
                b = bearing_deg(
                    interp_points[prev_i][0], interp_points[prev_i][1],
                    interp_points[next_i][0],  interp_points[next_i][1]
                )
                hotspots.append({
                    "lat": lat, "lon": lon,
                    "bearing": b, "run_len": run_len
                })
            i = j
        else:
            i += 1

    hotspots.sort(key=lambda h: h["run_len"], reverse=True)
    return hotspots[:max_hotspots]


async def _shade_targeted_routes(
    o: str,
    d: str,
    hotspots: list[dict],
    client: httpx.AsyncClient,
    max_dist: float,
    seen_polylines: set,
    transport: str = "foot"
) -> list[dict]:
    """
    Для каждой «солнечной горячей точки» пробуем боковые обходы
    на 80 и 160 м влево и вправо (перпендикулярно маршруту).
    2 горячих точки × 2 смещения × 2 стороны = 8 OSRM-запросов параллельно.
    """
    tasks = []
    for h in hotspots:
        lat, lon, bearing = h["lat"], h["lon"], h["bearing"]
        for offset_m in [80, 160]:
            for side_deg in [-90, 90]:
                wlat, wlon = offset_point(lat, lon, offset_m, (bearing + side_deg) % 360)
                wp = f"{o};{wlon},{wlat};{d}"
                tasks.append(_osrm_single(wp, client, alternatives=False, transport=transport))

    if not tasks:
        return []

    new_routes = []
    for res in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(res, list):
            for r in res:
                if r["polyline"] not in seen_polylines and r["distance"] <= max_dist:
                    seen_polylines.add(r["polyline"])
                    new_routes.append(r)
    return new_routes

# ─── Helpers ──────────────────────────────────────────────────────────────────

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
) -> list[tuple[dict, dict, list]]:
    """Анализирует список сырых маршрутов, возвращает (raw, stats, interp_points)."""
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
        result.append((raw, stats, pts))
    return result


def _make_route_out(idx: int, raw: dict, stats: dict) -> RouteOut:
    return RouteOut(
        id            = f"route-{idx+1}",
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


def _shade_fraction(r: RouteOut) -> float:
    total = r.metrics.shade_minutes + r.metrics.sun_minutes
    return r.metrics.shade_minutes / total if total > 0 else 0.0

# ─── Endpoint ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.post("/routes", response_model=RoutesResponse)
async def routes(req: RoutesRequest):
    if not rl.allow(req.client.device_id):
        raise HTTPException(429, "RATE_LIMIT")

    depart_dt = _parse_depart(req.departure_time)
    clat = (req.origin.lat + req.destination.lat) / 2
    clon = (req.origin.lon + req.destination.lon) / 2

    # ── 1. Маршруты + погода ──────────────────────────────────────────────────
    async with httpx.AsyncClient() as client:
        raw_routes, weather = await asyncio.gather(
            _osrm_routes(req.origin, req.destination, client, transport=req.transport),
            fetch_weather(clat, clon, client)
        )

    sun_alt, sun_az = sun_position(clat, clon, depart_dt)
    log.info(f"Sun: alt={sun_alt:.1f}° az={sun_az:.1f}° | routes={len(raw_routes)}")

    # ── 2. Здания (покрывают все варианты маршрутов, кэш 10 мин) ─────────────
    all_coords = [c for r in raw_routes for c in r["coords"]]
    async with httpx.AsyncClient() as client:
        buildings = await _fetch_buildings_cached(all_coords, client)
    log.info(f"Buildings: {len(buildings)}")

    loop = asyncio.get_event_loop()

    # ── 3. Первый проход анализа ──────────────────────────────────────────────
    analyzed = await _analyse_batch(
        raw_routes, buildings, sun_alt, sun_az, weather,
        req.walk_speed_mps, depart_dt, loop
    )

    best_shade = max(s["shade_fraction"] for _, s, _ in analyzed) if analyzed else 0.0
    log.info(f"Pass 1 best shade: {best_shade:.1%}")

    # ── 4. Второй проход: целевые детуры вокруг солнечных участков ────────────
    if best_shade < 0.50 and buildings:
        best_raw, best_stats, best_pts = max(
            analyzed, key=lambda x: x[1]["shade_fraction"]
        )
        shade_map = best_stats.get("shade_map", "")

        if shade_map:
            hotspots = _sun_hotspots(best_pts, shade_map)
            log.info(f"Hotspots: {hotspots}")

            if hotspots:
                o        = f"{req.origin.lon},{req.origin.lat}"
                d        = f"{req.destination.lon},{req.destination.lat}"
                max_dist = raw_routes[0]["distance"] * 1.7
                seen     = {r["polyline"] for r in raw_routes}

                async with httpx.AsyncClient() as client:
                    new_raws = await _shade_targeted_routes(
                        o, d, hotspots, client, max_dist, seen,
                        transport=req.transport
                    )
                log.info(f"Targeted detours: {len(new_raws)}")

                if new_raws:
                    new_analyzed = await _analyse_batch(
                        new_raws, buildings, sun_alt, sun_az, weather,
                        req.walk_speed_mps, depart_dt, loop
                    )
                    analyzed.extend(new_analyzed)
                    new_best = max(s["shade_fraction"] for _, s, _ in new_analyzed)
                    log.info(f"Pass 2 best shade: {new_best:.1%}")

    # ── 5. Ограничение по длительности по спецификации пресетов ──────────────
    #   less_uv  = «Максимум тени»: duration ≤ 1.7× кратчайшего
    #   cooler   = «Баланс тень/время»: duration ≤ 1.15× кратчайшего
    #   fast     = «Самый быстрый»: без ограничения
    min_dur = min(r["duration"] for r, _, _ in analyzed) if analyzed else 1
    dur_limit = {
        "less_uv": min_dur * 1.7,
        "cooler":  min_dur * 1.15,
        "fast":    float("inf"),
    }.get(req.preset, float("inf"))

    # ── 6. Строим RouteOut (дедупликация по polyline + нормализованная дистанция)
    out_routes: list[RouteOut] = []
    seen_poly:  set[str] = set()
    seen_dist:  list[int] = []   # сохраняем дистанции принятых маршрутов

    def _is_near_duplicate(dist_m: int) -> bool:
        """Считаем маршруты дубликатами, если дистанция отличается < 1%."""
        for d in seen_dist:
            if abs(dist_m - d) / max(d, 1) < 0.01:
                return True
        return False

    for i, (raw, stats, _) in enumerate(analyzed):
        if raw["polyline"] in seen_poly:
            continue
        if raw["duration"] > dur_limit:
            continue
        dist = stats["distance_m"]
        if _is_near_duplicate(dist):
            continue
        seen_poly.add(raw["polyline"])
        seen_dist.append(dist)
        out_routes.append(_make_route_out(i, raw, stats))

    # Гарантируем хотя бы 1 маршрут
    if not out_routes and analyzed:
        raw, stats, _ = min(analyzed, key=lambda x: x[0]["duration"])
        out_routes.append(_make_route_out(0, raw, stats))

    # ── 7. Сортировка по пресету ──────────────────────────────────────────────
    if req.preset == "less_uv":
        out_routes.sort(key=lambda r: r.metrics.sun_minutes)
    elif req.preset == "cooler":
        max_sun = max(r.metrics.sun_minutes for r in out_routes) or 1
        max_dur = max(r.duration_s for r in out_routes) or 1
        out_routes.sort(key=lambda r:
            0.6 * r.metrics.sun_minutes / max_sun +
            0.4 * r.duration_s / max_dur)
    else:
        out_routes.sort(key=lambda r: r.duration_s)

    # ── 8. Маршруты с ≥50% тени — всегда первые ──────────────────────────────
    out_routes.sort(key=lambda r: (0 if _shade_fraction(r) >= 0.5 else 1))

    return RoutesResponse(routes=out_routes[:5])
