"""Balad tools: place search, nearby and along-route places, place details and reviews, geocoding, driving routes."""

from __future__ import annotations

import asyncio
import html
import math
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from .http import DEVICE_ID, DIRECTION, POI, REVERSE, SEARCH, SITE, ApiError, fetch, search_headers
from .registry import tool

TEHRAN = (35.699521, 51.411954)  # balad.ir's default map center when no point is given

Lat = Annotated[float, Field(description="Latitude, e.g. 35.7448.", ge=24, le=40.5)]
Lng = Annotated[float, Field(description="Longitude, e.g. 51.3753.", ge=43, le=64)]
PlaceId = Annotated[
    str,
    Field(description="Place id from bl_search or bl_nearby.", pattern=r"^[A-Za-z0-9]{6,32}$"),
]
What = Annotated[
    str,
    Field(
        description="Category slug from bl_categories (e.g. pharmacy, gas-station) or free text (e.g. پیتزا).",
        min_length=1,
        max_length=100,
    ),
]
OpenNow = Annotated[bool, Field(description="Only places open now.")]

# Filter ids of balad.ir's list view (the same for every category, read from bundle-search `filters`).
SORT = {"relevance": None, "rating": "13", "popular": "14"}
MIN_RATING = {"3": "34", "4": "33", "4.5": "32"}
PRICE = {"cheap": "601", "moderate": "602", "expensive": "603"}  # restaurants and cafes only


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km, rounded to 0.1 km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return round(12742 * math.asin(math.sqrt(a)), 1)


def _view(lat: float, lng: float, radius_km: float) -> dict[str, str]:
    """The map view the web app sends: a closed lng,lat polygon around the point, the camera and a zoom level."""
    dlat = radius_km / 111.32
    dlng = radius_km / (111.32 * math.cos(math.radians(lat)))
    n, s, w, e = lat + dlat, lat - dlat, lng - dlng, lng + dlng
    ring = [(w, n), (e, n), (e, s), (w, s), (w, n)]
    zoom = next((z for r, z in ((0.5, 16), (1, 15), (2, 14), (5, 13), (10, 12)) if radius_km <= r), 10)
    return {
        "polygon": "|".join(f"{x:.6f},{y:.6f}" for x, y in ring),
        "zoom": str(zoom),
        "camera": f"{lng:.6f},{lat:.6f}",
    }


def _point(coords: Any) -> tuple[float, float] | None:
    """(lat, lng) from a GeoJSON [lng, lat] pair, or None."""
    if isinstance(coords, list) and len(coords) == 2 and all(isinstance(c, int | float) for c in coords):
        return round(coords[1], 6), round(coords[0], 6)
    return None


def _place_url(token: str, url_title: str | None) -> str:
    return f"{SITE}/p/{url_title}-{token}" if url_title else f"{SITE}/p/{token}"


def _clean(s: str) -> str:
    """Drop zero-width marks (U+200B/E/F, U+FEFF) and stray ZWNJ; ZWNJ between letters is Persian spelling and stays."""
    s = re.sub("[\u200b\u200e\u200f\ufeff]", "", s)
    s = re.sub("\u200c{2,}", "\u200c", s)
    return re.sub("(?<!\\w)\u200c|\u200c(?!\\w)", "", s)


def _text(s: Any, limit: int = 600) -> str:
    """Plain text from the site's HTML snippets and user text, cleaned and clipped."""
    t = _clean(html.unescape(re.sub(r"<[^>]+>", " ", str(s or ""))))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _review(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "rate": c.get("rate"),
        "when": c.get("created_at"),
        "user": _text(c.get("user_name"), 60),
        "text": _text(c.get("comment"), 400),
    }


def _filters(
    open_now: bool = False,
    min_rating: str | None = None,
    sort: str = "relevance",
    with_phone: bool = False,
    with_photos: bool = False,
    price: str | None = None,
) -> dict[str, str]:
    """balad.ir list-view filters as query params (f_open=21 ...)."""
    f = {
        "f_open": "21" if open_now else None,
        "f_rate": MIN_RATING.get(min_rating or ""),
        "f_sort": SORT.get(sort),
        "f_phone": "61" if with_phone else None,
        "f_pics": "41" if with_photos else None,
        "f_price_range": PRICE.get(price or ""),
    }
    return {k: v for k, v in f.items() if v}


async def _list(what: str, view: dict[str, str], filters: dict[str, str]) -> tuple[str | None, list[str], list[Any]]:
    """(category slug, place tokens, GeoJSON features) for `what` in one map view, as balad.ir lists them."""
    slug = what if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", what) else None
    body: Any = None
    if not slug:
        body = await fetch(f"{SEARCH}/v4/submit/", {"text": what, **view, **filters}, search_headers())
        # Like balad.ir: text that names a category ("پیتزا" -> fast-food-restaurant) lists that category.
        slug = body.get("bundle_slug") if isinstance(body, dict) else None
    if slug:
        body = await fetch(f"{SEARCH}/v4/bundle-search/", {"slug": slug, **view, **filters}, {"platform": "web"})
    body = body if isinstance(body, dict) else {}
    tokens = body.get("poi-tokens")
    tokens = [t for t in tokens if isinstance(t, str)] if isinstance(tokens, list) else []
    features = (body.get("geojson") or {}).get("features") if isinstance(body.get("geojson"), dict) else None
    return slug or None, tokens, features if isinstance(features, list) else []


async def _previews(tokens: list[str]) -> list[dict[str, Any]]:
    """Compact cards (name, address, phone, rating, lat/lng) for place tokens, in the given order."""
    if not tokens:
        return []
    bulk = await fetch(f"{POI}/preview-bulk/{','.join(tokens)}")
    items = bulk.get("items") if isinstance(bulk, dict) else None
    out = []
    for p in items if isinstance(items, list) else []:
        if not isinstance(p, dict) or not p.get("token"):
            continue
        rating = p.get("rating") if isinstance(p.get("rating"), dict) else {}
        row: dict[str, Any] = {
            "id": p["token"],
            "name": p.get("name"),
            "category": p.get("category"),
            "address": p.get("address"),
            "phone": p.get("telephone"),
            "rating": rating.get("score"),
            "ratings": rating.get("count"),
        }
        if pt := _point((p.get("geometry") or {}).get("coordinates")):
            row["lat"], row["lng"] = pt
        out.append(row)
    return out


@tool("Search places and addresses")
async def bl_search(
    query: Annotated[
        str,
        Field(
            description="Place name, street, address, neighbourhood or city, e.g. برج میلاد.",
            min_length=1,
            max_length=200,
        ),
    ],
    lat: Annotated[
        float | None, Field(description="Latitude to search around. Default: Tehran.", ge=24, le=40.5)
    ] = None,
    lng: Annotated[
        float | None, Field(description="Longitude to search around. Default: Tehran.", ge=43, le=64)
    ] = None,
    limit: Annotated[int, Field(description="Max results (1-20).", ge=1, le=20)] = 10,
) -> dict[str, Any]:
    """Search Balad (Iranian map) for places, streets, neighbourhoods and cities by name, with coordinates and address.

    Also geocodes addresses and city names. Pass lat/lng when searching outside Tehran (nearer results rank
    first). `type` is poi (pass its `id` to bl_place), road, city, region or category (its `category` slug goes
    to bl_nearby). For all places of one kind use bl_nearby. Empty: try a shorter or Persian query.
    """
    clat, clng = (lat, lng) if lat is not None and lng is not None else TEHRAN
    body = await fetch(f"{SEARCH}/v6/", {"text": query, **_view(clat, clng, 25)}, search_headers())
    rows = body.get("results") if isinstance(body, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("type") == "query_suggestion" or not r.get("maintext"):
            continue
        kind = r.get("type")
        row: dict[str, Any] = {"type": "category" if r.get("bundle_slug") else kind, "name": r["maintext"]}
        if r.get("bundle_slug"):
            row["category"] = r["bundle_slug"]
        elif kind == "poi":
            row["id"] = r.get("id")
        row["address"] = r.get("subtext1")
        if pt := _point(r.get("center_point")):
            row["lat"], row["lng"] = pt
            if lat is not None and lng is not None:
                row["distance_km"] = _km(lat, lng, *pt)
        row["phone"] = r.get("phone")
        out.append(row)
        if len(out) >= limit:
            break
    return {"results": out}


@tool("Nearby places by category")
async def bl_nearby(
    what: What,
    lat: Lat,
    lng: Lng,
    radius_km: Annotated[float, Field(description="Box half-width in km (0.2-20).", ge=0.2, le=20)] = 1,
    limit: Annotated[int, Field(description="Max places (1-40).", ge=1, le=40)] = 10,
    open_now: OpenNow = False,
    min_rating: Annotated[Literal["3", "4", "4.5"] | None, Field(description="Only places rated above this.")] = None,
    sort: Annotated[
        Literal["relevance", "rating", "popular"],
        Field(description="rating = best first, popular = most visited."),
    ] = "relevance",
    with_phone: Annotated[bool, Field(description="Only places with a phone number.")] = False,
    with_photos: Annotated[bool, Field(description="Only places with photos.")] = False,
    price: Annotated[
        Literal["cheap", "moderate", "expensive"] | None,
        Field(description="Restaurants and cafes only."),
    ] = None,
) -> dict[str, Any]:
    """List places of one kind (pharmacy, restaurant, gas station...) around a point, with address, phone and rating.

    Balad's own order, each with straight-line `distance_km`. Free text naming a category (پیتزا) lists that
    whole category (`category` says which). Get the point from bl_search. For driving time use
    bl_nearest_by_drive; for places on a trip bl_along_route; for hours and reviews bl_place with `id`. Empty:
    widen radius_km or drop filters.
    """
    filters = _filters(open_now, min_rating, sort, with_phone, with_photos, price)
    slug, tokens, _ = await _list(what, _view(lat, lng, radius_km), filters)
    places = await _previews(tokens[:limit])
    for p in places:
        if "lat" in p:
            p["distance_km"] = _km(lat, lng, p["lat"], p["lng"])
    applied = {
        "open_now": open_now or None,
        "min_rating": min_rating,
        "sort": sort if sort != "relevance" else None,
        "with_phone": with_phone or None,
        "with_photos": with_photos or None,
        "price": price,
    }
    applied = {k: v for k, v in applied.items() if v} or None
    return {"category": slug, "filters": applied, "found": len(tokens), "places": places}


def _fields(fields: Any) -> dict[str, Any]:
    """Address, phones, links, opening hours and about-text from the place page's `fields` blocks."""
    out: dict[str, Any] = {}
    for f in fields if isinstance(fields, list) else []:
        if not isinstance(f, dict):
            continue
        kind, icon, value = f.get("type"), f.get("icon"), f.get("value")
        if kind == "text" and icon == "gps":
            out["address"] = value
        elif kind == "link" and icon == "phone":
            out.setdefault("phones", []).append(f.get("text") or str(value).removeprefix("tel://"))
        elif kind == "link" and icon in ("web", "instagram", "telegram", "whatsapp", "email"):
            out.setdefault("links", {})[icon] = value
        elif kind == "html":
            out["about"] = _text(value)
        elif kind == "dropdown" and icon == "clock":
            for sub in f.get("fields") or []:
                if isinstance(sub, dict) and sub.get("type") == "list":
                    out["hours"] = dict(zip(sub.get("titles") or [], sub.get("items") or [], strict=False))
            if f.get("message"):
                out["hours_status"] = f["message"]
    return out


async def _amenities(p: dict[str, Any], token: str) -> list[str]:
    """Amenity names; the full list comes from trait-groups when the page says there are more."""
    main = p.get("main_traits") if isinstance(p.get("main_traits"), dict) else {}
    names = [t.get("title") for t in main.get("items") or [] if isinstance(t, dict)]
    names += [t.get("name") for t in p.get("traits") or [] if isinstance(t, dict)]
    if main.get("has_more"):
        try:
            groups = (await fetch(f"{POI}/pois/{token}/trait-groups")).get("groups") or []
            names = [i.get("title") for g in groups if isinstance(g, dict) for i in g.get("items") or []]
        except (ApiError, AttributeError):
            pass  # the short list is still right
    return list(dict.fromkeys(_text(n, 80) for n in names if n))


@tool("Place details")
async def bl_place(
    id: PlaceId,
    reviews: Annotated[int, Field(description="Recent reviews to include (0-10).", ge=0, le=10)] = 3,
    photos: Annotated[int, Field(description="Photo URLs to include (0-20).", ge=0, le=20)] = 3,
) -> dict[str, Any]:
    """Get one place's full details from Balad: address, phones, hours, open now, rating, amenities, photos, reviews.

    `hours` maps each weekday (شنبه..جمعه) to its hours; `menu` is the menu link. More reviews: bl_reviews;
    directions: bl_route with its lat/lng.
    """
    try:
        p, state = await asyncio.gather(
            fetch(f"{POI}/{id}", extra={"device-id": DEVICE_ID}), fetch(f"{POI}/is-open/{id}")
        )
    except ApiError as e:
        if e.status == 404:
            raise ApiError(f"No place with id {id}. Get ids from bl_search or bl_nearby.", 404) from e
        raise
    if not isinstance(p, dict) or not p.get("token"):
        raise ApiError(f"Balad returned no details for place {id}.")
    rating = p.get("rating") if isinstance(p.get("rating"), dict) else {}
    seo = p.get("seo_details") if isinstance(p.get("seo_details"), dict) else {}
    out: dict[str, Any] = {"id": p["token"], "name": p.get("name"), "category": p.get("category")}
    if pt := _point((p.get("geometry") or {}).get("coordinates")):
        out["lat"], out["lng"] = pt
    out.update(_fields(p.get("fields")))
    if isinstance(state, dict) and "open" in state:
        out["open_now"] = state.get("open")
        out["hours_status"] = state.get("message") or out.get("hours_status")
    out["rating"] = rating.get("score")
    out["ratings"] = rating.get("count")
    out["price"] = (p.get("price_range") or {}).get("title") if isinstance(p.get("price_range"), dict) else None
    menu = p.get("menu") if isinstance(p.get("menu"), dict) else {}
    out["menu"] = (menu.get("link") or {}).get("href") if isinstance(menu.get("link"), dict) else None
    out["amenities"] = await _amenities(p, p["token"])
    images = [i for i in p.get("images") or [] if isinstance(i, dict) and i.get("full")]
    out["photo_count"] = len(images) or None
    out["photos"] = [i["full"] for i in images[:photos]]
    rv = p.get("reviews") if isinstance(p.get("reviews"), dict) else {}
    if rv.get("rate_details"):
        out["stars_1_to_5"] = rv["rate_details"]
    out["review_count"] = rv.get("total_comments")
    comments = [c for c in rv.get("comments") or [] if isinstance(c, dict)]
    out["reviews"] = [_review(c) for c in comments[:reviews]]
    out["url"] = _place_url(p["token"], seo.get("url_title"))
    return out


@tool("Place reviews")
async def bl_reviews(
    id: PlaceId,
    page: Annotated[int, Field(description="Page (10 reviews each, 1-100).", ge=1, le=100)] = 1,
) -> dict[str, Any]:
    """Get one page of user reviews of a place on Balad, newest first, with stars and the rating breakdown.

    When `more` is true, call again with page+1. bl_place already includes the first few reviews.
    """
    try:
        body = await fetch(f"{POI}/all-comments/{id}/10/{page}")
    except ApiError as e:
        if e.status == 404:
            raise ApiError(f"No place with id {id}. Get ids from bl_search or bl_nearby.", 404) from e
        raise
    if not isinstance(body, dict):
        raise ApiError(f"Balad returned no reviews for place {id}.")
    comments = [c for c in body.get("comments") or [] if isinstance(c, dict)]
    total = body.get("total_comments") or 0
    return {
        "id": id,
        "rating": body.get("average_rate"),
        "ratings": body.get("total"),
        "stars_1_to_5": body.get("rate_details"),
        "review_count": total,
        "page": page,
        "more": page * 10 < total,
        "reviews": [_review(c) for c in comments],
    }


@tool("Address of a point")
async def bl_reverse_geocode(lat: Lat, lng: Lng) -> dict[str, Any]:
    """Turn coordinates into a Persian street address with city, county and neighbourhood (reverse geocoding).

    For the opposite (name -> coordinates) use bl_search. Iran only; elsewhere it errors.
    """
    body = await fetch(REVERSE, {"location": f"{lng},{lat}"})
    addr = body.get("formatted_address") if isinstance(body, dict) else None
    if not addr:
        raise ApiError("Balad has no address for this point (it covers Iran only). Check the lat/lng order.")
    out: dict[str, Any] = {"address": addr}
    for c in body.get("components") or []:
        if not isinstance(c, dict) or not c.get("full_name"):
            continue
        if c.get("type") == "street":
            out.setdefault("streets", []).append(c["full_name"])
        elif c.get("type"):
            out.setdefault(c["type"], c["full_name"])
    return out


async def _drive(points: list[tuple[float, float]]) -> dict[str, Any]:
    """Balad's driving-traffic answer for (lat, lng) points in order; a clear error when there is no route."""
    path = ";".join(f"{lng},{lat}" for lat, lng in points)
    try:
        body = await fetch(f"{DIRECTION}/{path}")
    except ApiError as e:
        if e.status in (400, 422):
            raise ApiError(
                "Balad found no driving route between these points (it covers Iran only). Check the coordinates.",
                e.status,
            ) from e
        raise
    body = body if isinstance(body, dict) else {}
    routes = body.get("routes")
    if body.get("code") != "Ok" or not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise ApiError(f"Balad found no driving route: {body.get('message') or 'empty answer'}.")
    return body


def _minutes(seconds: Any) -> int:
    return int((seconds or 0) // 60)  # whole minutes, as balad.ir shows them


def _steps(route: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for leg in route.get("legs") or []:
        for s in leg.get("steps") or [] if isinstance(leg, dict) else []:
            m = s.get("maneuver") if isinstance(s, dict) else None
            if not isinstance(m, dict) or not m.get("instruction"):
                continue
            out.append(
                {"do": m["instruction"], "road": s.get("name") or None, "km": round((s.get("distance") or 0) / 1000, 2)}
            )
    return out


def _traffic_plan(route: dict[str, Any]) -> dict[str, Any] | None:
    """The site's warning when a route enters a traffic-restriction zone (طرح ترافیک, زوج و فرد)."""
    r = route.get("restriction") if isinstance(route.get("restriction"), dict) else None
    if not r:
        return None
    zones = [
        " ".join(x for x in (z.get("full_title"), f"({z['message']})" if z.get("message") else None) if x)
        for z in r.get("restrictions") or []
        if isinstance(z, dict) and z.get("is_active", True)
    ]
    return {"message": r.get("message"), "zones": zones}


Stop = Annotated[str, Field(pattern=r"^\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*$")]


@tool("Driving route with live traffic")
async def bl_route(
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    stops: Annotated[
        list[Stop],
        Field(description='Stops in order, each "lat,lng" (max 3).', max_length=3),
    ] = [],  # noqa: B006  (pydantic copies defaults)
    steps: Annotated[bool, Field(description="Include turn-by-turn steps.")] = False,
) -> dict[str, Any]:
    """Get driving routes in Iran with distance, travel time in current traffic, tolls, roads and traffic-zone warnings.

    Up to 3 routes, suggested first; `notes` are the site's labels (delay, toll in Toman); `traffic_plan` appears
    when a route enters a restricted zone (طرح ترافیک, زوج و فرد); `legs` with `stops`. Driving only. Get
    coordinates from bl_search. 'No route': a point is outside Iran or off the road network.
    """
    via = [tuple(float(x) for x in s.split(",")) for s in stops]
    if any(not (24 <= a <= 40.5 and 43 <= b <= 64) for a, b in via):
        raise ApiError('Each stop must be "lat,lng" inside Iran, e.g. "35.7016,51.4206" (latitude first).')
    body = await _drive([(from_lat, from_lng), *via, (to_lat, to_lng)])  # type: ignore[list-item]
    out_routes = []
    for i, r in enumerate(r for r in body["routes"] if isinstance(r, dict)):
        tags = [t.get("title") for t in r.get("tags") or [] if isinstance(t, dict) and t.get("title")]
        row: dict[str, Any] = {
            "title": tags[0] if tags else None,
            "km": round((r.get("distance") or 0) / 1000, 1),
            "minutes": _minutes(r.get("duration")),
            "notes": [x["title"] for x in r.get("routeLabels") or [] if isinstance(x, dict) and x.get("title")],
            "roads": r.get("message") or None,
            "traffic_plan": _traffic_plan(r),
        }
        legs = [g for g in r.get("legs") or [] if isinstance(g, dict)]
        if len(legs) > 1:
            row["legs"] = [
                {"km": round((g.get("distance") or 0) / 1000, 1), "minutes": _minutes(g.get("duration"))} for g in legs
            ]
        if steps and i == 0:
            row["steps"] = _steps(r)
        out_routes.append(row)
    return {
        "from": body.get("sourceTitle") or None,
        "to": body.get("destinationTitle") or None,
        "routes": out_routes,
    }


@tool("Closest places by driving time")
async def bl_nearest_by_drive(
    what: What,
    lat: Lat,
    lng: Lng,
    radius_km: Annotated[float, Field(description="Box half-width in km (0.5-20).", ge=0.5, le=20)] = 3,
    candidates: Annotated[int, Field(description="How many top places to time (2-8).", ge=2, le=8)] = 5,
    open_now: OpenNow = False,
) -> dict[str, Any]:
    """Rank places of one kind around a point by driving time from it in current traffic, quickest first.

    Times a drive to Balad's top `candidates` places (as bl_nearby lists them), one route call each; unroutable
    places are left out. For more places without drive times use bl_nearby; details: bl_place.
    """
    slug, tokens, _ = await _list(what, _view(lat, lng, radius_km), _filters(open_now))
    places = [p for p in await _previews(tokens[:candidates]) if "lat" in p]

    async def time(p: dict[str, Any]) -> dict[str, Any] | None:
        try:
            r = (await _drive([(lat, lng), (p["lat"], p["lng"])]))["routes"][0]
        except ApiError:
            return None
        return {
            **p,
            "drive_minutes": _minutes(r.get("duration")),
            "drive_km": round((r.get("distance") or 0) / 1000, 1),
        }

    timed = [p for p in await asyncio.gather(*(time(p) for p in places)) if p]
    timed.sort(key=lambda p: (p["drive_minutes"], p["drive_km"]))
    return {"category": slug, "found": len(tokens), "timed": len(timed), "places": timed}


def _polyline(s: str, precision: int = 6) -> list[tuple[float, float]]:
    """Decode an encoded polyline (Balad's route geometry is polyline6) into (lat, lng) points."""
    points, idx, lat, lng = [], 0, 0, 0
    while idx < len(s):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lng = lat + deltas[0], lng + deltas[1]
        points.append((lat / 10**precision, lng / 10**precision))
    return points


def _along(line: list[tuple[float, float]], lat: float, lng: float, cum: list[float]) -> tuple[float, float]:
    """(km from the start along the line, km off the line) for a point, using a local flat projection."""
    kx = 111.32 * math.cos(math.radians(lat))
    best = (math.inf, 0.0)
    for i in range(len(line) - 1):
        (y1, x1), (y2, x2) = line[i], line[i + 1]
        ax, ay, bx, by = (x1 - lng) * kx, (y1 - lat) * 111.32, (x2 - lng) * kx, (y2 - lat) * 111.32
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
        off = math.hypot(ax + t * dx, ay + t * dy)
        if off < best[0]:
            best = (off, cum[i] + t * (cum[i + 1] - cum[i]))
    return best[1], best[0]


MAX_SAMPLES = 12
MAX_BOX_KM = 5


@tool("Places along a driving route")
async def bl_along_route(
    what: What,
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    max_off_route_km: Annotated[
        float, Field(description="Max distance from the road in km (0.2-3).", ge=0.2, le=3)
    ] = 0.5,
    limit: Annotated[int, Field(description="Max places to return (1-30).", ge=1, le=30)] = 10,
    open_now: OpenNow = False,
) -> dict[str, Any]:
    """Find places of one kind (gas station, restaurant, pharmacy...) along the suggested driving route between two points.

    Ordered by `km_from_start`, with `off_route_km`. Searches up to 12 boxes along the route, so on trips of
    hundreds of km places between boxes are missed; split the trip for full coverage.
    """
    route = (await _drive([(from_lat, from_lng), (to_lat, to_lng)]))["routes"][0]
    line = _polyline(route.get("geometry") or "")
    if len(line) < 2:
        raise ApiError("Balad returned a route without geometry; try again.")
    cum = [0.0]
    for (a, b), (c, d) in zip(line, line[1:], strict=False):
        cum.append(cum[-1] + _km(a, b, c, d) if (a, b) != (c, d) else cum[-1])
    total = cum[-1] or 0.1
    r = max_off_route_km
    spacing = max(2 * r, total / (MAX_SAMPLES - 1))
    box = min(max(r, spacing / 2), MAX_BOX_KM)  # boxes touch along the route; places are then kept within r
    samples, k = [], 0.0
    while k <= total and len(samples) < MAX_SAMPLES:
        i = next(j for j, c in enumerate(cum) if c >= k)
        samples.append(line[i])
        k += spacing
    filters = _filters(open_now)
    slug, _, _ = await _list(what, _view(*samples[0], box), filters)  # resolve text to a category once
    lists = await asyncio.gather(*(_list(slug or what, _view(la, lo, box), filters) for la, lo in samples))
    found: dict[str, tuple[float, float]] = {}
    for _, tokens, features in lists:
        for t, f in zip(tokens, features, strict=False):
            pt = _point((f.get("geometry") or {}).get("coordinates")) if isinstance(f, dict) else None
            if pt and t not in found:
                at, off = _along(line, *pt, cum)
                if off <= r:
                    found[t] = (at, off)
    order = sorted(found, key=lambda t: found[t][0])[:limit]
    places = await _previews(order)
    for p in places:
        at, off = found.get(p["id"], (None, None))
        p["km_from_start"] = round(at, 1) if at is not None else None
        p["off_route_km"] = round(off, 2) if off is not None else None
    places.sort(key=lambda p: p["km_from_start"] if p["km_from_start"] is not None else math.inf)
    return {
        "category": slug,
        "route": {
            "km": round((route.get("distance") or 0) / 1000, 1),
            "minutes": _minutes(route.get("duration")),
            "roads": route.get("message") or None,
        },
        "searched_every_km": round(spacing, 1),
        "found": len(found),
        "places": places,
    }


@tool("Place categories")
async def bl_categories() -> dict[str, Any]:
    """List Balad's place categories with the slug bl_nearby takes, grouped (food, health, shopping, services...).

    Use it for the exact slug for bl_nearby, bl_nearest_by_drive or bl_along_route.
    """
    body = await fetch(f"{SEARCH}/v6/bundle-list/full/")
    groups = body.get("results") if isinstance(body, dict) else None
    out = {}
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict) and g.get("title"):
            cats = [c for c in g.get("categories") or [] if isinstance(c, dict) and c.get("slug")]
            out[g["title"]] = {c["slug"]: (c.get("name") or "").strip() for c in cats}
    if not out:
        raise ApiError("Balad returned no categories.")
    return {"groups": out}
