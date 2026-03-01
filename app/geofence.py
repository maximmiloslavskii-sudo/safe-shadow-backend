BA = {  # Buenos Aires bbox (approx)
    "name": "buenos_aires",
    "lat_min": -34.75,
    "lat_max": -34.50,
    "lon_min": -58.55,
    "lon_max": -58.30,
}

MOSCOW = {  # Moscow bbox (approx)
    "name": "moscow",
    "lat_min": 55.50,
    "lat_max": 55.95,
    "lon_min": 37.30,
    "lon_max": 38.10,
}

def in_box(lat: float, lon: float, box: dict) -> bool:
    return (box["lat_min"] <= lat <= box["lat_max"]) and (box["lon_min"] <= lon <= box["lon_max"])

def city_lock(origin: dict, destination: dict):
    for box in (BA, MOSCOW):
        if in_box(origin["lat"], origin["lon"], box) and in_box(destination["lat"], destination["lon"], box):
            return box["name"]
    return None
