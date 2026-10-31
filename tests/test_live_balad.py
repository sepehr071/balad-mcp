import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]

MILAD = "4Dvh6E79N49I2j"  # برج میلاد تهران


async def test_live_search(client):
    r = (await client.call_tool("bl_search", {"query": "برج میلاد", "limit": 5})).structured_content
    assert any(x.get("id") == MILAD for x in r["results"]), r


async def test_live_search_city_for_geocoding(client):
    r = (await client.call_tool("bl_search", {"query": "شیراز", "limit": 3})).structured_content
    city = next(x for x in r["results"] if x["type"] == "city")
    assert 29 < city["lat"] < 30.5 and 52 < city["lng"] < 53


async def test_live_nearby_category(client):
    r = (
        await client.call_tool("bl_nearby", {"what": "pharmacy", "lat": 35.7, "lng": 51.4, "limit": 5})
    ).structured_content
    assert r["found"] > 0 and len(r["places"]) == 5
    assert all(p["distance_km"] < 3 and p["category"] == "داروخانه" for p in r["places"]), r


async def test_live_nearby_free_text(client):
    r = (
        await client.call_tool("bl_nearby", {"what": "پیتزا", "lat": 29.61, "lng": 52.5383, "radius_km": 2})
    ).structured_content
    assert r["found"] > 0 and r["places"][0]["address"].startswith("شیراز"), r


async def test_live_place(client):
    r = (await client.call_tool("bl_place", {"id": MILAD})).structured_content
    assert r["name"] == "برج میلاد تهران" and r["phones"] and r["hours"] and r["rating"] > 3
    assert isinstance(r["open_now"], bool) and r["reviews"]


async def test_live_reviews(client):
    r = (await client.call_tool("bl_reviews", {"id": MILAD, "page": 2})).structured_content
    assert len(r["reviews"]) == 10 and r["more"] is True


async def test_live_reverse_geocode(client):
    r = (await client.call_tool("bl_reverse_geocode", {"lat": 35.744645, "lng": 51.375452})).structured_content
    assert "برج میلاد" in r["address"] and r["city"] == "شهر تهران"


async def test_live_route(client):
    args = {"from_lat": 35.744645, "from_lng": 51.375452, "to_lat": 35.699739, "to_lng": 51.338076, "steps": True}
    r = (await client.call_tool("bl_route", args)).structured_content
    best = r["routes"][0]
    assert 9 < best["km"] < 16 and 8 < best["minutes"] < 90 and best["steps"]


async def test_live_route_with_stop(client):
    args = {"from_lat": 35.744645, "from_lng": 51.375452, "to_lat": 35.699739, "to_lng": 51.338076}
    r = (await client.call_tool("bl_route", args | {"stops": ["35.7016,51.4206"]})).structured_content
    best = r["routes"][0]
    assert len(best["legs"]) == 2 and abs(sum(g["km"] for g in best["legs"]) - best["km"]) < 0.3


async def test_live_nearby_filters(client):
    args = {"what": "restaurant", "lat": 35.7, "lng": 51.4, "radius_km": 2, "min_rating": "4", "sort": "rating"}
    r = (await client.call_tool("bl_nearby", args)).structured_content
    assert r["found"] > 0 and all(p.get("rating", 0) > 4 for p in r["places"]), r


async def test_live_place_photos_and_amenities(client):
    r = (await client.call_tool("bl_place", {"id": "7lQafcX3qGieD5", "photos": 2})).structured_content
    assert len(r["photos"]) == 2 and r["amenities"] and r["hours"]


async def test_live_nearest_by_drive(client):
    args = {"what": "gas-station", "lat": 35.7448, "lng": 51.3753, "candidates": 3}
    r = (await client.call_tool("bl_nearest_by_drive", args)).structured_content
    times = [p["drive_minutes"] for p in r["places"]]
    assert r["timed"] >= 2 and times == sorted(times)


async def test_live_along_route(client):
    args = {"what": "gas-station", "from_lat": 35.744645, "from_lng": 51.375452, "to_lat": 35.8327, "to_lng": 50.9915}
    r = (await client.call_tool("bl_along_route", args)).structured_content
    km = [p["km_from_start"] for p in r["places"]]
    assert r["places"] and km == sorted(km) and all(p["off_route_km"] <= 0.5 for p in r["places"])


async def test_live_categories(client):
    r = (await client.call_tool("bl_categories", {})).structured_content
    assert sum(len(g) for g in r["groups"].values()) > 50
