import httpx
import pytest
from conftest import fixture

from balad_mcp import http

pytestmark = pytest.mark.anyio

MILAD = "4Dvh6E79N49I2j"
ROUTE = "/navigation-v7/directions/v5/mapbox/driving-traffic/51.375452,35.744645;51.338076,35.699739"
DRIVE = "/navigation-v7/directions/v5/mapbox/driving-traffic/"
TOOLS = {
    "bl_search",
    "bl_nearby",
    "bl_place",
    "bl_reviews",
    "bl_reverse_geocode",
    "bl_route",
    "bl_nearest_by_drive",
    "bl_along_route",
    "bl_categories",
}


def bulk_path(name: str) -> str:
    tokens = fixture(name)["poi-tokens"]
    return "/web/v4/preview-bulk/" + ",".join(tokens)


async def test_all_tools_are_read_only_and_described(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.title and t.description.splitlines()[0].endswith("."), t.name
        assert "title" not in str(t.input_schema.get("properties", {}).values()), t.name


async def test_search_returns_places_with_ids_and_coordinates(client, api):
    api["/v6/"] = fixture("search.json")
    r = (await client.call_tool("bl_search", {"query": "برج میلاد", "limit": 3})).structured_content
    first = r["results"][0]
    assert len(r["results"]) == 3 and first["type"] == "poi" and first["id"] == MILAD
    assert (first["lat"], first["lng"]) == (35.744645, 51.375452) and "distance_km" not in first
    q = api.calls[0].url.params
    assert q["text"] == "برج میلاد" and q["camera"] == "51.411954,35.699521"  # lng,lat; Tehran by default
    ring = q["polygon"].split("|")
    assert len(ring) == 5 and ring[0] == ring[-1]


async def test_search_near_a_point_adds_distance(client, api):
    api["/v6/"] = fixture("search.json")
    r = (await client.call_tool("bl_search", {"query": "برج میلاد", "lat": 35.7, "lng": 51.4})).structured_content
    assert r["results"][0]["distance_km"] == 5.4
    assert api.calls[0].url.params["camera"] == "51.400000,35.700000"


async def test_search_category_row_points_to_nearby(client, api):
    api["/v6/"] = fixture("search_category.json")
    r = (await client.call_tool("bl_search", {"query": "داروخانه"})).structured_content
    row = r["results"][0]
    assert (row["type"], row["name"], row["category"]) == ("category", "داروخانه", "pharmacy") and "id" not in row


async def test_nearby_by_category_slug(client, api):
    api["/v4/bundle-search/"] = fixture("bundle_search.json")
    api[bulk_path("bundle_search.json")] = fixture("preview_bulk.json")
    r = (
        await client.call_tool("bl_nearby", {"what": "pharmacy", "lat": 35.7, "lng": 51.4, "limit": 5})
    ).structured_content
    assert r["category"] == "pharmacy" and r["found"] == 5 and len(r["places"]) == 5
    p = r["places"][0]
    assert p["name"] == "داروخانه دکتر افشین مهاجر" and p["phone"] and p["distance_km"] == 0.1
    search = api.calls[0]
    assert search.headers["platform"] == "web" and search.url.params["slug"] == "pharmacy"
    assert search.url.params["zoom"] == "15"


async def test_nearby_limit_trims_details_call(client, api):
    api["/v4/bundle-search/"] = fixture("bundle_search.json")
    tokens = fixture("bundle_search.json")["poi-tokens"][:2]
    api["/web/v4/preview-bulk/" + ",".join(tokens)] = {"items": fixture("preview_bulk.json")["items"][:2]}
    r = (
        await client.call_tool("bl_nearby", {"what": "pharmacy", "lat": 35.7, "lng": 51.4, "limit": 2})
    ).structured_content
    assert r["found"] == 5 and len(r["places"]) == 2


async def test_nearby_text_naming_a_category_lists_the_category(client, api):
    api["/v4/submit/"] = fixture("submit.json")  # پیتزا -> bundle fast-food-restaurant
    api["/v4/bundle-search/"] = fixture("bundle_search.json")
    api[bulk_path("bundle_search.json")] = fixture("preview_bulk.json")
    r = (await client.call_tool("bl_nearby", {"what": "پیتزا", "lat": 29.61, "lng": 52.5383})).structured_content
    assert r["category"] == "fast-food-restaurant" and r["found"] == 5 and len(r["places"]) == 5
    submit, bundle = api.calls[0], api.calls[1]
    assert submit.headers["device-id"] == http.DEVICE_ID and submit.url.params["text"] == "پیتزا"
    assert (
        bundle.url.params["slug"] == "fast-food-restaurant"
        and bundle.url.params["polygon"] == submit.url.params["polygon"]
    )


async def test_nearby_other_text_lists_matching_places(client, api):
    items = fixture("preview_bulk.json")["items"][:2]
    api["/v4/submit/"] = {"poi-tokens": [i["token"] for i in items], "bundle_id": "-1", "bundle_slug": ""}
    api["/web/v4/preview-bulk/" + ",".join(i["token"] for i in items)] = {"items": items}
    r = (await client.call_tool("bl_nearby", {"what": "برج میلاد", "lat": 35.7, "lng": 51.4})).structured_content
    assert r["category"] is None and r["found"] == 2 and [p["id"] for p in r["places"]] == [i["token"] for i in items]
    assert len(api.calls) == 2


async def test_nearby_nothing_found(client, api):
    api["/v4/submit/"] = {"poi-tokens": [], "bundle_slug": ""}
    r = (await client.call_tool("bl_nearby", {"what": "xyz qq", "lat": 35.7, "lng": 51.4})).structured_content
    assert r["found"] == 0 and r["places"] == [] and len(api.calls) == 1


async def test_place_details(client, api):
    api[f"/web/v4/{MILAD}"] = fixture("poi.json")
    api[f"/web/v4/is-open/{MILAD}"] = fixture("is_open.json")
    r = (await client.call_tool("bl_place", {"id": MILAD, "reviews": 2})).structured_content
    assert r["name"] == "برج میلاد تهران" and r["category"] == "جاذبه گردشگری"
    assert r["phones"] == ["0218585"] and r["links"]["web"] == "https://miladticket.com/"
    assert r["address"].startswith("تهران") and len(r["hours"]) == 7 and r["open_now"] is True
    assert r["rating"] == 4.6 and len(r["reviews"]) == 2 and r["reviews"][0]["text"]
    assert "<" not in r["about"] and r["url"].endswith(f"_tourist-attractions-{MILAD}")
    assert next(c for c in api.calls if c.url.path == f"/web/v4/{MILAD}").headers["device-id"] == http.DEVICE_ID


async def test_place_unknown_id(client, api):
    api["/web/v4/zzzzzzzzzzzzzz"] = lambda r: httpx.Response(404, text="error")
    api["/web/v4/is-open/zzzzzzzzzzzzzz"] = lambda r: httpx.Response(404, text="error")
    r = await client.call_tool("bl_place", {"id": "zzzzzzzzzzzzzz"})
    assert r.is_error and "No place with id" in r.content[0].text


async def test_place_rejects_bad_id(client, api):
    assert (await client.call_tool("bl_place", {"id": "../../x"})).is_error
    assert api.calls == []


async def test_reviews_page(client, api):
    api[f"/web/v4/all-comments/{MILAD}/10/2"] = fixture("comments.json")
    r = (await client.call_tool("bl_reviews", {"id": MILAD, "page": 2})).structured_content
    assert r["page"] == 2 and r["more"] is True and len(r["reviews"]) == 10
    assert set(r["reviews"][0]) <= {"rate", "when", "user", "text"} and len(r["stars_1_to_5"]) == 5


async def test_reverse_geocode(client, api):
    api["/v1"] = fixture("reverse.json")
    r = (await client.call_tool("bl_reverse_geocode", {"lat": 35.744645, "lng": 51.375452})).structured_content
    assert r["address"].startswith("تهران") and r["city"] == "شهر تهران" and r["streets"]
    assert api.calls[0].url.params["location"] == "51.375452,35.744645"


async def test_reverse_geocode_outside_iran(client, api):
    api["/v1"] = {"formatted_address": "", "components": []}
    r = await client.call_tool("bl_reverse_geocode", {"lat": 25.0, "lng": 60.0})
    assert r.is_error and "Iran only" in r.content[0].text


async def test_route(client, api):
    api[ROUTE] = fixture("route.json")
    args = {"from_lat": 35.744645, "from_lng": 51.375452, "to_lat": 35.699739, "to_lng": 51.338076}
    r = (await client.call_tool("bl_route", args)).structured_content
    best = r["routes"][0]
    assert r["to"] == "میدان آزادی" and len(r["routes"]) == 3
    assert (best["km"], best["minutes"], best["title"]) == (12.0, 21, "مسیر پیشنهادی")
    assert "کمترین مصرف سوخت" in best["notes"] and "steps" not in best
    r = (await client.call_tool("bl_route", args | {"steps": True})).structured_content
    steps = r["routes"][0]["steps"]
    assert steps[0]["do"] and any(s.get("road") == "میدان آزادی" for s in steps) and "steps" not in r["routes"][1]


async def test_route_not_found(client, api):
    api[ROUTE] = lambda r: httpx.Response(422, json={"message": "مسیری پیدا نشد", "result": ""})
    args = {"from_lat": 35.744645, "from_lng": 51.375452, "to_lat": 35.699739, "to_lng": 51.338076}
    r = await client.call_tool("bl_route", args)
    assert r.is_error and "no driving route" in r.content[0].text


async def test_route_with_stops_and_traffic_plan(client, api):
    api[DRIVE + "51.375452,35.744645;51.4206,35.7016;51.338076,35.699739"] = fixture("route_stops.json")
    args = {"from_lat": 35.744645, "from_lng": 51.375452, "to_lat": 35.699739, "to_lng": 51.338076}
    r = (await client.call_tool("bl_route", args | {"stops": ["35.7016, 51.4206"]})).structured_content
    best = r["routes"][0]
    assert (best["km"], best["minutes"]) == (23.7, 53) and best["legs"] == [
        {"km": 15.2, "minutes": 33},
        {"km": 8.5, "minutes": 20},
    ]
    assert best["traffic_plan"] == {"message": "مسیر شما از طرح عبور می\u200cکنه.", "zones": ["طرح ترافیک (تا 18:00)"]}


async def test_route_rejects_stop_outside_iran(client, api):
    args = {"from_lat": 35.7, "from_lng": 51.3, "to_lat": 35.6, "to_lng": 51.3, "stops": ["51.42,35.70"]}
    r = await client.call_tool("bl_route", args)
    assert r.is_error and "latitude first" in r.content[0].text and api.calls == []


async def test_nearby_filters_use_the_sites_filter_ids(client, api):
    api["/v4/bundle-search/"] = fixture("bundle_search.json")
    api[bulk_path("bundle_search.json")] = fixture("preview_bulk.json")
    args = {"what": "restaurant", "lat": 35.7, "lng": 51.4, "open_now": True, "min_rating": "4", "sort": "rating"}
    r = (await client.call_tool("bl_nearby", args | {"with_phone": True, "price": "cheap"})).structured_content
    q = api.calls[0].url.params
    assert (q["f_open"], q["f_rate"], q["f_sort"], q["f_phone"], q["f_price_range"]) == ("21", "33", "13", "61", "601")
    assert "f_pics" not in q
    assert r["filters"] == {"open_now": True, "min_rating": "4", "sort": "rating", "with_phone": True, "price": "cheap"}


async def test_place_menu_photos_and_amenities(client, api):
    token = "21n5izaolvOtcI"
    p = fixture("poi_restaurant.json")
    p["main_traits"] = {"items": [{"title": "پیک"}], "has_more": True}
    api[f"/web/v4/{token}"] = p
    api[f"/web/v4/is-open/{token}"] = {"open": False, "message": "بسته است"}
    api[f"/web/v4/pois/{token}/trait-groups"] = fixture("trait_groups.json")
    r = (await client.call_tool("bl_place", {"id": token, "photos": 2})).structured_content
    assert r["menu"] == "https://sefareshfood.com/menu/SepahRestaurant-restaurant-teh"
    assert len(r["photos"]) == 2 and r["photos"][0].startswith("https://cdn.balad.ir/") and r["photo_count"] == 4
    assert r["amenities"] == ["پیک"] and r["open_now"] is False


async def test_review_text_loses_zero_width_marks_but_keeps_zwnj():
    from balad_mcp.maps import _clean

    raw = "\u0639\u0627\u0644\u06cc\u200c\u200e\u200b\u200e\u200c\u200b \u0645\u06cc\u200f\u200f \u0628\u0627\u0634\u062f \ufeff\u0646\u06cc\u0645\u200c\u0641\u0627\u0635\u0644\u0647"
    assert (
        _clean(raw)
        == "\u0639\u0627\u0644\u06cc \u0645\u06cc \u0628\u0627\u0634\u062f \u0646\u06cc\u0645\u200c\u0641\u0627\u0635\u0644\u0647"
    )  # the ZWNJ inside the last word stays


def _route_points(n: int) -> list[tuple[float, float]]:
    from balad_mcp.maps import _polyline

    return _polyline(fixture("route_geometry.json")["routes"][0]["geometry"])[:: max(1, n)]


async def test_along_route_keeps_places_on_the_road_in_route_order(client, api):
    api[DRIVE + "51.375452,35.744645;51.338076,35.699739"] = fixture("route_geometry.json")
    line = _route_points(1)
    near_end, near_start = line[-20], line[15]
    far = (35.80, 51.50)
    pts = {"AAAAAAend1": near_end, "AAAAAstart": near_start, "AAAAAAfar1": far}
    body = {
        "bundle_slug": "gas-station",
        "poi-tokens": list(pts),
        "geojson": {"features": [{"geometry": {"coordinates": [lng, lat]}} for lat, lng in pts.values()]},
    }
    api["/v4/bundle-search/"] = body
    items = [{"token": t, "name": t, "geometry": {"coordinates": [lng, lat]}} for t, (lat, lng) in pts.items()]
    api["/web/v4/preview-bulk/AAAAAstart,AAAAAAend1"] = {"items": [items[1], items[0]]}
    args = {"what": "gas-station", "from_lat": 35.744645, "from_lng": 51.375452, "to_lat": 35.699739}
    r = (await client.call_tool("bl_along_route", args | {"to_lng": 51.338076})).structured_content
    assert r["route"]["km"] == 12.9 and r["found"] == 2
    assert [p["id"] for p in r["places"]] == ["AAAAAstart", "AAAAAAend1"]
    first, last = r["places"]
    assert first["off_route_km"] == 0 and 0 < first["km_from_start"] < last["km_from_start"] <= 13
    searches = [c for c in api.calls if c.url.path == "/v4/bundle-search/"]
    assert 2 <= len(searches) <= 13 and all(c.url.params["slug"] == "gas-station" for c in searches)


async def test_nearest_by_drive_sorts_by_drive_time(client, api):
    api["/v4/bundle-search/"] = fixture("bundle_search.json")
    items = fixture("preview_bulk.json")["items"][:3]
    api["/web/v4/preview-bulk/" + ",".join(i["token"] for i in items)] = {"items": items}
    minutes = [9, 3, 6]
    for item, m in zip(items, minutes, strict=True):
        lng, lat = item["geometry"]["coordinates"]
        route = {"code": "Ok", "routes": [{"distance": m * 400, "duration": m * 60 + 30}]}
        api[f"{DRIVE}51.4,35.7;{round(lng, 6)},{round(lat, 6)}"] = route
    args = {"what": "pharmacy", "lat": 35.7, "lng": 51.4, "candidates": 3}
    r = (await client.call_tool("bl_nearest_by_drive", args)).structured_content
    assert [p["drive_minutes"] for p in r["places"]] == [3, 6, 9] and r["timed"] == 3
    assert r["places"][0]["id"] == items[1]["token"] and r["places"][0]["drive_km"] == 1.2


async def test_categories(client, api):
    api["/v6/bundle-list/full/"] = fixture("categories.json")
    r = (await client.call_tool("bl_categories", {})).structured_content
    assert r["groups"]["سلامت"]["pharmacy"] == "داروخانه"
    assert "gas-station" in r["groups"]["خدمات خودرو"]


async def test_requests_look_like_the_web_app(client, api):
    api["/v6/"] = fixture("search.json")
    await client.call_tool("bl_search", {"query": "x"})
    h = api.calls[0].headers
    assert h["origin"] == "https://balad.ir" and h["referer"] == "https://balad.ir/"
    assert h["accept"] == "application/json, text/plain, */*" and h["sec-fetch-site"] == "cross-site"
    assert h["sec-fetch-mode"] == "cors" and h["platform"] == "web"
    assert h["app-session"] == http.APP_SESSION and h["device-id"] == http.DEVICE_ID and h["search-session"]
    api["/v1"] = fixture("reverse.json")
    await client.call_tool("bl_reverse_geocode", {"lat": 35.7, "lng": 51.4})
    assert "device-id" not in api.calls[1].headers and "platform" not in api.calls[1].headers


async def test_answers_are_cached(client, api):
    api["/v6/bundle-list/full/"] = fixture("categories.json")
    await client.call_tool("bl_categories", {})
    await client.call_tool("bl_categories", {})
    assert len(api.calls) == 1


async def test_server_errors_are_retried_then_reported(client, api):
    api["/v1"] = lambda r: httpx.Response(503)
    r = await client.call_tool("bl_reverse_geocode", {"lat": 35.7, "lng": 51.4})
    assert r.is_error and "503" in r.content[0].text and len(api.calls) == http.MAX_RETRIES + 1


async def test_unexpected_host_is_refused():
    with pytest.raises(http.ApiError, match="unexpected host"):
        await http.fetch("https://example.com/x")


def test_transport_drops_reload_and_navigation_headers():
    t = http._XhrTransport(impersonate="chrome", default_headers=True)
    timeout = {"connect": 5, "read": 20, "write": 20, "pool": 20}
    req = httpx.Request("GET", "https://search.raah.ir/v6/", headers={"sec-fetch-mode": "cors"})
    req.extensions["timeout"] = timeout
    p = t._create_request_params(req)
    dropped = {k for k, v in p["headers"] if v is None}
    assert {b"cache-control", b"upgrade-insecure-requests", b"sec-fetch-user"} <= dropped
