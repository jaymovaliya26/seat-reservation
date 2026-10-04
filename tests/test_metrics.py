"""Metrics must agree with what the API answered. Tests compare deltas, because counters are
process-wide and other tests in the session move them too."""

import httpx
from prometheus_client.parser import text_string_to_metric_families

from app import __version__
from tests.conftest import create_show, user_headers


async def scrape(client: httpx.AsyncClient) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    samples = {}
    for family in text_string_to_metric_families(response.text):
        for sample in family.samples:
            samples[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return samples


def value(samples: dict, name: str, **labels: str) -> float:  # type: ignore[type-arg]
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


async def reserve(
    client: httpx.AsyncClient, show_id: str, user: str, seats: list[str], key: str | None = None
) -> httpx.Response:
    body: dict[str, object] = {"seats": seats}
    if key:
        body["idempotency_key"] = key
    return await client.post(f"/shows/{show_id}/reserve", json=body, headers=user_headers(user))


async def test_reserve_outcomes_are_counted_by_reason(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1", "A2", "A3", "A4"], per_user_limit=2)
    before = await scrape(client)

    assert (await reserve(client, show_id, "m-alice", ["A1", "A2"], key="m-1")).status_code == 201
    assert (await reserve(client, show_id, "m-alice", ["A1", "A2"], key="m-1")).status_code == 200
    assert (await reserve(client, show_id, "m-bob", ["A1"])).status_code == 409
    assert (await reserve(client, show_id, "m-alice", ["A3"])).status_code == 409
    assert (await reserve(client, show_id, "m-bob", ["Z9"])).status_code == 422

    after = await scrape(client)

    def delta(name: str, **labels: str) -> float:
        return value(after, name, **labels) - value(before, name, **labels)

    assert delta("reservations_confirmed_total") == 1
    assert delta("seats_sold_total") == 2
    assert delta("reservations_declined_total", reason="idempotent_replay") == 1
    assert delta("reservations_declined_total", reason="seat_taken") == 1
    assert delta("reservations_declined_total", reason="per_user_limit") == 1
    assert delta("reservations_declined_total", reason="unknown_seats") == 1


async def test_cancellations_are_counted(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1", "A2"])
    booked = (await reserve(client, show_id, "m-carol", ["A1", "A2"])).json()
    before = await scrape(client)

    await client.post(
        f"/reservations/{booked['reservation_id']}/cancel", headers=user_headers("m-carol")
    )

    after = await scrape(client)
    assert (
        value(after, "reservations_cancelled_total") - value(before, "reservations_cancelled_total")
        == 1
    )
    assert value(after, "seats_released_total") - value(before, "seats_released_total") == 2


async def test_seat_gauges_match_the_show(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1", "A2", "A3"])
    await reserve(client, show_id, "m-dave", ["A2"])

    samples = await scrape(client)
    counts = (await client.get(f"/shows/{show_id}")).json()["counts"]

    assert value(samples, "seats_available", show_id=show_id) == counts["available"] == 2
    assert value(samples, "seats_confirmed", show_id=show_id) == counts["confirmed"] == 1
    assert value(samples, "seats_held", show_id=show_id) == counts["held"] == 0
    assert value(samples, "seats_total", show_id=show_id) == counts["total"] == 3
    assert value(samples, "seats_invariant_ok", show_id=show_id) == 1


async def test_http_metrics_use_route_templates(client: httpx.AsyncClient) -> None:
    show_id = await create_show(client, ["A1"])
    await client.get(f"/shows/{show_id}")

    samples = await scrape(client)

    routes = {
        dict(labels).get("route") for (name, labels) in samples if name == "http_requests_total"
    }
    assert "/shows/{show_id}" in routes
    assert not any(show_id in str(route) for route in routes)  # no per-show label explosion
    assert value(samples, "app_info", version=__version__) == 1
