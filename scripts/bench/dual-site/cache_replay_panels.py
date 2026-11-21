"""Five-second dashboard refresh after completion, with an independent fixed ingest stream."""

import asyncio
from collections import defaultdict
import time


def split_plan(events):
    panels = defaultdict(list)
    writes = []
    for event in events:
        if event["kind"] == "write":
            writes.append(event)
        else:
            panels[event["panel"]].append(event)
    if len(panels) != 16:
        raise ValueError("The feedback workload needs sixteen individually identified panels")
    return panels, writes


def next_due(event, previous_finish):
    return event["due_s"] if previous_finish is None else previous_finish + 5


def effective_plan(nominal, rows, horizon):
    """Rebuild which offers were due, including the end boundary, solely from receipts."""
    panels, writes = split_plan(nominal)
    actual = {r["sequence"]: r for r in rows}
    if len(actual) != len(rows):
        raise ValueError("Duplicate feedback receipt")
    scheduled = list(writes)
    for events in panels.values():
        previous = None
        for event in events:
            due = next_due(event, previous)
            if due >= horizon:
                break
            row = actual.get(event["sequence"])
            if row is None or row["finished_s"] < due or abs(row["due_s"] - due) > 0.00001:
                raise ValueError("Missing or mistimed refresh on a panel")
            scheduled.append(dict(event, due_s=due))
            previous = row.get("list_finished_s", row["finished_s"])
    scheduled.sort(key=lambda e: e["sequence"])
    if [r["sequence"] for r in rows] != [e["sequence"] for e in scheduled]:
        raise ValueError("Feedback receipts differ from the eligible panel refreshes and writes")
    return scheduled


async def run(events, cell, horizon, process, guard):
    """Run one loop per panel. Delay starts never change the offered write schedule."""
    panels, writes = split_plan(events)
    t0 = time.monotonic()
    rows = cell["arrivals"]

    async def wait_until(due):
        await asyncio.sleep(max(0, t0 + due - time.monotonic()))

    async def panel(events):
        previous = None
        for event in events:
            due = next_due(event, previous)
            if due >= horizon:
                break
            await wait_until(due)
            row = await asyncio.to_thread(process, dict(event, due_s=due), t0)
            rows.append(row)
            previous = row.get("list_finished_s", row["finished_s"])

    async def send_write(event):
        await wait_until(event["due_s"])
        rows.append(await asyncio.to_thread(process, event, t0))

    async def clients():
        async with asyncio.TaskGroup() as tasks:
            for offers in panels.values():
                tasks.create_task(panel(offers))
            for event in writes:
                tasks.create_task(send_write(event))

    async with asyncio.TaskGroup() as group:
        task = group.create_task(clients())
        while not task.done():
            guard()
            await asyncio.wait([task], timeout=0.25)
    rows.sort(key=lambda r: r["sequence"])
    # Check temperature at least once after the last response as well.
    guard()
    return effective_plan(events, rows, horizon)
