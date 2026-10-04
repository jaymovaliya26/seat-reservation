#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""On-sale stampede against a running deployment, followed by a full correctness report.

    ./scripts/burst.sh <BASE_URL> [ADMIN_KEY]                   # the one command
    uv run scripts/burst.py <BASE_URL> --admin-key KEY [--requests 20000] [--concurrency 500]

A fresh show goes on sale and ~20,000 reserve requests arrive at once, mixed like a real on-sale:

  hot      40%  everyone wants the same 5 seats: exactly one winner each, everyone else 409
  overlap  15%  2-3 seats from a small band, in random order: no deadlocks, no split bookings
  retries  15%  each idempotency key sent 5 times at once: one booking per key, the rest replay
  limit         50 users fire 10 parallel requests each at a limit of 4: exactly 4 seats each
  spoof         body says user_id "victim": the booking must belong to the token's user
  general  rest single random seats

Then: late retries and key reuse, the seat counts polled throughout the burst, /metrics deltas
compared with the responses, and the server's own reconcile audit. Exit code 1 if anything fails.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import aiohttp

HOT_SEATS = 5
OVERLAP_BAND = 40
RETRY_SENDS = 5
GREEDY_USERS, GREEDY_REQUESTS, LIMIT = 50, 10, 4
SPOOFS = 100
GENERAL_BAND = 1000

GATE = asyncio.Semaphore(500)  # replaced in main() with --concurrency


@dataclass
class Result:
    scenario: str
    user: str
    seats: list[str]
    key: str | None
    status: int  # 0 = transport error or timeout
    body: dict = field(default_factory=dict)
    latency: float = 0.0


@dataclass
class Plan:
    seats: list[str]
    groups: list[
        list[tuple[str, str, list[str], str | None, dict]]
    ]  # (scenario, user, seats, key, extra)
    users: set[str]


def build_plan(total: int, rng: random.Random) -> Plan:
    hot = [f"H{n}" for n in range(1, HOT_SEATS + 1)]
    overlap = [f"O{n}" for n in range(1, OVERLAP_BAND + 1)]
    retry_keys = max(1, int(total * 0.15) // RETRY_SENDS)
    retry = [f"R{n}" for n in range(1, retry_keys + 1)]
    greedy = [f"G{n}" for n in range(1, GREEDY_USERS * GREEDY_REQUESTS + 1)]
    spoof = [f"S{n}" for n in range(1, SPOOFS + 1)]
    general = [f"X{n}" for n in range(1, GENERAL_BAND + 1)]

    groups: list[list[tuple[str, str, list[str], str | None, dict]]] = []
    hot_n, overlap_n = int(total * 0.40), int(total * 0.15)
    hot_users = [f"hot-{n}" for n in range(max(1, hot_n // 4))]
    for i in range(hot_n):
        groups.append([("hot", hot_users[i % len(hot_users)], [hot[i % HOT_SEATS]], None, {})])
    overlap_users = [f"ovl-{n}" for n in range(max(1, overlap_n // 2))]
    for i in range(overlap_n):
        wanted = rng.sample(overlap, rng.randint(2, 3))
        groups.append([("overlap", overlap_users[i % len(overlap_users)], wanted, None, {})])
    for i, seat in enumerate(retry):  # each key's sends stay together so they really race
        user, key = f"rty-{i}", f"burst-{uuid.uuid4().hex[:12]}"
        groups.append([("retry", user, [seat], key, {})] * RETRY_SENDS)
    for u in range(GREEDY_USERS):
        mine = greedy[u * GREEDY_REQUESTS : (u + 1) * GREEDY_REQUESTS]
        groups.append([("limit", f"greedy-{u}", [seat], None, {}) for seat in mine])
    for i, seat in enumerate(spoof):
        groups.append([("spoof", f"atk-{i}", [seat], None, {"user_id": "victim"})])
    used = sum(len(g) for g in groups)
    general_users = [f"gen-{n}" for n in range(max(1, (total - used) // 3))]
    for i in range(max(0, total - used)):
        groups.append(
            [("general", general_users[i % len(general_users)], [rng.choice(general)], None, {})]
        )

    rng.shuffle(groups)
    users = {request[1] for group in groups for request in group} | {"late-reuse"}
    return Plan(seats=hot + overlap + retry + greedy + spoof + general, groups=groups, users=users)


def metric_values(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith(("reservations_confirmed_total", "reservations_declined_total")):
            name, _, value = line.rpartition(" ")
            values[name] = float(value)
    return values


async def main() -> int:
    global GATE
    args = parse_args()
    GATE = asyncio.Semaphore(args.concurrency)
    base = args.base_url.rstrip("/")
    rng = random.Random(args.seed)
    plan = build_plan(args.requests, rng)
    checks: list[tuple[bool, str]] = []

    def check(ok: bool, text: str) -> None:
        checks.append((ok, text))

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=args.concurrency, ttl_dns_cache=300)
    async with aiohttp.ClientSession(base, connector=connector, timeout=timeout) as http:
        async with http.get("/readyz") as r:
            if r.status != 200:
                print(f"{base} is not ready: /readyz returned {r.status}")
                return 1

        admin = {"X-Admin-Key": args.admin_key}
        async with http.post(
            "/shows",
            json={
                "name": f"burst-{time.strftime('%H%M%S')}",
                "seats": plan.seats,
                "price_paise": 25_000,
                "per_user_limit": LIMIT,
            },
            headers=admin,
        ) as r:
            if r.status != 201:
                print(f"Could not create the show ({r.status}): {await r.text()}")
                return 1
            show_id = (await r.json())["id"]

        print(f"Show {show_id}: {len(plan.seats)} seats, per-user limit {LIMIT}")
        print(f"Getting tokens for {len(plan.users)} users ...", flush=True)
        tokens = await mint_tokens(http, sorted(plan.users), args.concurrency)

        async with http.get("/metrics") as r:
            metrics_before = metric_values(await r.text())

        # Watch the seat counts while the burst runs, on a connection of its own.
        stop = asyncio.Event()
        snapshots: list[dict] = []
        watcher = asyncio.create_task(watch_counts(base, show_id, stop, snapshots))

        total = sum(len(g) for g in plan.groups)
        print(
            f"Firing {total} reserve requests, up to {args.concurrency} at a time ...", flush=True
        )
        started = time.perf_counter()
        results = await asyncio.gather(
            *(
                send(http, show_id, tokens, scenario, user, seats, key, extra)
                for group in plan.groups
                for (scenario, user, seats, key, extra) in group
            )
        )
        elapsed = time.perf_counter() - started
        stop.set()
        await watcher

        # Late retries: the same body must replay, a different body must be refused.
        retry_results = [r for r in results if r.scenario == "retry" and r.status in (200, 201)]
        by_key = {r.key: r for r in retry_results}
        late_keys = list(by_key)[:100]
        late = await asyncio.gather(
            *(
                send(http, show_id, tokens, "late-retry", by_key[k].user, by_key[k].seats, k, {})
                for k in late_keys[:50]
            ),
            *(
                send(http, show_id, tokens, "late-reuse", by_key[k].user, ["X1"], k, {})
                for k in late_keys[50:]
            ),
        )

        await asyncio.sleep(0.5)
        async with http.get(f"/shows/{show_id}") as r:
            final = await r.json() if r.status == 200 else {"error": r.status}
        async with http.get("/metrics") as r:
            metrics_after = metric_values(await r.text())
        async with http.get(f"/admin/shows/{show_id}/reconcile", headers=admin) as r:
            audit = await r.json() if r.status == 200 else {"ok": False, "error": r.status}

    everything = list(results) + list(late)
    report_outcomes(results, late, elapsed)
    evaluate(
        results, late, snapshots, final, audit, metrics_before, metrics_after, everything, check
    )

    print("\nChecks")
    for ok, text in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    failed = [text for ok, text in checks if not ok]
    print(f"\n{'ALL CHECKS PASSED' if not failed else f'{len(failed)} CHECK(S) FAILED'}")
    return 1 if failed else 0


async def mint_tokens(
    http: aiohttp.ClientSession, users: list[str], concurrency: int
) -> dict[str, str]:
    gate = asyncio.Semaphore(concurrency)

    async def one(user: str) -> tuple[str, str]:
        async with gate, http.post("/auth/token", json={"user_id": user}) as r:
            r.raise_for_status()
            return user, (await r.json())["access_token"]

    return dict(await asyncio.gather(*(one(u) for u in users)))


async def send(
    http: aiohttp.ClientSession,
    show_id: str,
    tokens: dict[str, str],
    scenario: str,
    user: str,
    seats: list[str],
    key: str | None,
    extra: dict,
) -> Result:
    headers = {"Authorization": f"Bearer {tokens[user]}"}
    if key:
        headers["Idempotency-Key"] = key
    # Latency is timed from when the request has a connection slot, so it measures the server
    # and the network, not this script's own queue.
    async with GATE:
        started = time.perf_counter()
        try:
            async with http.post(
                f"/shows/{show_id}/reserve", json={"seats": seats, **extra}, headers=headers
            ) as r:
                body = await r.json(content_type=None)
                status = r.status
        except (TimeoutError, aiohttp.ClientError) as exc:
            body, status = {"client_error": repr(exc)}, 0
        return Result(scenario, user, seats, key, status, body or {}, time.perf_counter() - started)


async def watch_counts(base: str, show_id: str, stop: asyncio.Event, out: list[dict]) -> None:
    async with aiohttp.ClientSession(base) as http:
        while not stop.is_set():
            try:
                async with http.get(f"/shows/{show_id}") as r:
                    if r.status == 200:
                        out.append((await r.json())["counts"])
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)


def status_mix(rows: list[Result]) -> dict[int, int]:
    return dict(sorted(Counter(r.status for r in rows).items()))


def code_of(r: Result) -> str:
    if r.status == 200:
        return "idempotent_replay"
    return r.body.get("error", {}).get("code", f"http_{r.status}")


def report_outcomes(results: list[Result], late: list[Result], elapsed: float) -> None:
    statuses = Counter(r.status for r in results)
    latencies = sorted(r.latency for r in results)

    def pct(p: float) -> float:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] * 1000

    print(
        f"\nBurst: {len(results)} requests in {elapsed:.2f}s = {len(results) / elapsed:,.0f} req/s"
    )
    print(
        f"Latency: p50 {pct(0.50):.0f} ms, p95 {pct(0.95):.0f} ms, p99 {pct(0.99):.0f} ms, "
        f"max {latencies[-1] * 1000:.0f} ms, mean {statistics.fmean(latencies) * 1000:.0f} ms"
    )
    print("\nOutcomes")
    print(f"  confirmed (201)              {statuses[201]:>7}")
    declined = Counter(code_of(r) for r in results if r.status in (200, 409))
    for reason, n in declined.most_common():
        print(f"  declined: {reason:<20} {n:>7}")
    other = {s: n for s, n in statuses.items() if s not in (200, 201, 409)}
    print(f"  5xx                          {sum(n for s, n in statuses.items() if s >= 500):>7}")
    for status, n in sorted(other.items()):
        if status < 500:
            print(f"  {'client error/timeout' if status == 0 else f'other {status}':<28} {n:>7}")
    print("\nBy scenario")
    for scenario in ("hot", "overlap", "retry", "limit", "spoof", "general"):
        rows = [r for r in results if r.scenario == scenario]
        print(f"  {scenario:<8} {len(rows):>6} requests  {status_mix(rows)}")
    print(f"  late     {len(late):>6} requests  {status_mix(late)}")


def evaluate(results, late, snapshots, final, audit, before, after, everything, check) -> None:
    statuses = Counter(r.status for r in everything)
    server_errors = sum(n for s, n in statuses.items() if s >= 500)
    check(server_errors == 0, f"zero 5xx across {len(everything)} requests (saw {server_errors})")
    check(statuses[0] == 0, f"no client-side errors or timeouts (saw {statuses[0]})")
    unexpected = {s for s in statuses if s not in (0, 200, 201, 409) and s < 500}
    check(
        not unexpected,
        f"only 200/201/409 outcomes (other statuses: {sorted(unexpected) or 'none'})",
    )

    # 1. Hot seats: exactly one winner each.
    winners: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        if r.status == 201:
            for seat in r.body["seats"]:
                winners[seat].append(r)
    hot = sorted({r.seats[0] for r in results if r.scenario == "hot"})
    for seat in hot:
        n = len(winners[seat])
        tried = sum(1 for r in results if r.scenario == "hot" and r.seats[0] == seat)
        check(n == 1, f"hot seat {seat}: {tried} buyers, exactly one 201 (saw {n})")
    hot_losers = [r for r in results if r.scenario == "hot" and r.status != 201]
    check(
        all(r.status == 409 for r in hot_losers),
        f"all {len(hot_losers)} hot-seat losers got a clean 409",
    )

    # 2. No seat sold twice, across every scenario; every winner got all of its seats.
    double = [seat for seat, rs in winners.items() if len(rs) > 1]
    check(not double, f"no seat confirmed twice (doubles: {double[:5] or 'none'})")
    split = [r for r in results if r.status == 201 and sorted(r.seats) != r.body["seats"]]
    check(not split, f"every 201 holds exactly the seats requested ({len(split)} mismatches)")

    # 3. Idempotency: one booking per key, retries replay it.
    by_key: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        if r.key:
            by_key[r.key].append(r)
    keys_ok = all(
        sum(1 for r in rs if r.status == 201) == 1
        and len({r.body.get("reservation_id") for r in rs if r.status in (200, 201)}) == 1
        for rs in by_key.values()
    )
    sends = sum(len(rs) for rs in by_key.values())
    check(
        keys_ok,
        f"{len(by_key)} keys x {RETRY_SENDS} racing sends: one booking per key ({sends} requests)",
    )
    late_retry = [r for r in late if r.scenario == "late-retry"]
    late_reuse = [r for r in late if r.scenario == "late-reuse"]
    check(
        all(r.status == 200 for r in late_retry),
        f"{len(late_retry)} late retries replayed with 200",
    )
    check(
        all(code_of(r) == "idempotency_key_reused" for r in late_reuse),
        f"{len(late_reuse)} keys reused with other seats got 409 idempotency_key_reused",
    )

    # 4. Per-user limit under parallel requests.
    greedy: dict[str, int] = Counter(
        r.user for r in results if r.scenario == "limit" and r.status == 201
    )
    over = {u: n for u, n in greedy.items() if n > LIMIT}
    check(
        not over and len(greedy) == GREEDY_USERS,
        f"{GREEDY_USERS} users x {GREEDY_REQUESTS} parallel requests: none above {LIMIT} seats "
        f"(max {max(greedy.values(), default=0)})",
    )

    # 5. Identity from the token, not the body.
    spoofed = [
        r
        for r in results
        if r.scenario == "spoof" and r.status == 201 and r.body.get("user_id") != r.user
    ]
    check(
        not spoofed,
        f"spoofed body user_id ignored: bookings belong to the token's user ({len(spoofed)} wrong)",
    )

    # 6. The invariant held at every moment we looked, and at the end.
    bad = [c for c in snapshots if c["available"] + c["held"] + c["confirmed"] != c["total"]]
    check(
        snapshots and not bad,
        f"available + held + confirmed == total in all {len(snapshots)} mid-burst snapshots",
    )
    c = final.get("counts")
    sold = sum(len(r.body["seats"]) for r in results if r.status == 201)
    if c is None:
        check(False, f"final GET /shows/{{id}} failed ({final})")
    else:
        check(
            c["available"] + c["held"] + c["confirmed"] == c["total"],
            f"final counts add up: {c['available']} + {c['held']} + {c['confirmed']} "
            f"= {c['total']}",
        )
        check(
            c["confirmed"] == sold,
            f"confirmed seats ({c['confirmed']}) equal seats in 201 responses ({sold})",
        )

    # 7. Metrics agree with what clients were told (exact unless other traffic hit the server).
    def delta(name: str) -> int:
        return round(after.get(name, 0) - before.get(name, 0))

    confirmed = sum(1 for r in everything if r.status == 201)
    check(
        delta("reservations_confirmed_total") == confirmed,
        f"/metrics reservations_confirmed_total +{delta('reservations_confirmed_total')} "
        f"== {confirmed} responses with 201",
    )
    reasons = Counter(code_of(r) for r in everything if r.status in (200, 409))
    for reason, n in sorted(reasons.items()):
        d = delta(f'reservations_declined_total{{reason="{reason}"}}')
        check(d == n, f"/metrics declined{{{reason}}} +{d} == {n} responses")

    # 8. The server's own audit.
    failing = [name for name, chk in audit.get("checks", {}).items() if not chk["ok"]]
    check(audit.get("ok") is True, f"reconcile audit ok (failing checks: {failing or 'none'})")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("base_url")
    p.add_argument("--admin-key", required=True)
    p.add_argument("--requests", type=int, default=20_000)
    p.add_argument("--concurrency", type=int, default=500)
    p.add_argument("--timeout", type=float, default=60.0, help="per-request timeout in seconds")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    if not re.match(r"^https?://", args.base_url):
        p.error("BASE_URL must start with http:// or https://")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
