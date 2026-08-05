"""The corpus generator: a month of service logs with defects planted in it.

Nothing here is committed except this file. The logs are regenerated from a seed
into the session's workspace, which keeps a 54MB artifact out of git and makes
"reproducible" mean *derivable* rather than *archived*.

**Why a dirty corpus.** Summing a column is not evidence of anything — a shell
one-liner does it, and an agent that does it has demonstrated arithmetic. What
separates an agent that ran a groupby from one that read its data first is
whether it notices the data is lying, so every question this suite asks has a
specific, reachable *wrong* answer that a competent-looking shortcut produces.
The five defects below are each load-bearing for exactly one question; a defect
that changes no answer is decoration and would have been cut.

**Why truth is read back off disk.** `generate()` writes the files and then
`derive()` reads the bytes back and computes both the correct answers and the
naive ones from the same artifact. Deriving truth from the generator's
*intent* instead would let the board publish a discrimination the data does not
actually contain — the answer key and the data would drift apart silently, and
the suite would keep printing green. `verify()` asserts the naive and correct
answers still differ, so a parameter tweak that flattens the discrimination
fails at generation rather than at scoring time.

Two things that look like fussiness and are not:

- `random.Random(seed)` is an *instance*. Module-level `random` shares state
  with anything else in the process, so a test that seeds it elsewhere silently
  changes the corpus.
- `mtime=0` on every `GzipFile`. gzip stores a timestamp in its header, so
  identical rows otherwise produce different bytes on every run and the corpus
  stops being byte-reproducible.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

# --- the shape of the month ------------------------------------------------

YEAR, MONTH, DAYS = 2026, 6, 30

# Traffic follows a diurnal curve peaking here. This is the answer to the third
# question, and the timezone defect is what makes it non-trivial.
PEAK_HOUR_UTC = 15

# The SLO the second question is asked against. Well clear of both the healthy
# p95 (~300ms) and the regressed one (~780ms), so the answer never depends on
# which percentile method anyone used.
SLO_MS = 500

# --- where each defect starts ----------------------------------------------

TZ_SHIFT_DAY = 8  # ts becomes local time, keeps the Z suffix
TZ_OFFSET_HOURS = -4  # America/New_York in June
RENAME_DAY = 10  # `route` -> `path`
DUPLICATE_DAY = 14  # the day whose export ran twice
UNITS_DAY = 19  # latency_ms starts holding microseconds
REGRESSION_DAY = 25  # the genuine regression: the answer to question 2
MALFORMED_LINES = 50  # truncated writes, scattered

DEFECTS = {
    "timezone-shift": (
        f"from day {TZ_SHIFT_DAY} timestamps are local time (UTC{TZ_OFFSET_HOURS:+d}) "
        "still suffixed 'Z'"
    ),
    "field-rename": f"from day {RENAME_DAY} the 'route' key is named 'path'",
    "duplicate-export": f"day {DUPLICATE_DAY} was exported twice into one file",
    "unit-change": (
        f"from day {UNITS_DAY} 'latency_ms' holds microseconds, name unchanged"
    ),
    "malformed-lines": "truncated lines scattered through the month",
}

# --- traffic mix -----------------------------------------------------------

# (route, share, median latency ms, spread). /health is the bulk of the volume
# and none of the signal, which is what a real log looks like.
ROUTES = (
    ("/health", 0.34, 4, 0.35),
    ("/api/products", 0.20, 45, 0.50),
    ("/search", 0.16, 90, 0.60),
    ("/cart", 0.15, 70, 0.55),
    ("/checkout", 0.10, 120, 0.557),
    ("/login", 0.05, 60, 0.45),
)

UPSTREAMS = {
    "/checkout": "payments-gw",
    "/cart": "cart-svc",
    "/login": "auth-svc",
    "/search": "search-svc",
    "/api/products": "catalog-svc",
    "/health": "-",
}

# What the regression does to /checkout from REGRESSION_DAY on. 2.6x takes the
# p95 from ~300ms to ~780ms: unambiguously over the SLO, and unambiguously not
# reachable by day 24's noise.
REGRESSION_FACTOR = 2.6


@dataclass(frozen=True)
class Truth:
    """The answer key, plus what each shortcut yields instead.

    The `naive_*` fields are not commentary. They are the board's evidence that
    a wrong answer was *available* — an arm that returns the correct number when
    the wrong one was equally reachable has demonstrated something, and an arm
    that returns the wrong one can be told exactly which defect ate it.
    """

    requests: int
    breach_day: int
    peak_hour: int

    naive_requests: int
    naive_breach_day: int
    naive_peak_hour: int

    rows_written: int
    bytes_uncompressed: int
    # Counted off disk, not assumed from MALFORMED_LINES. A defect that failed
    # to land is a question that silently became free.
    malformed: int = 0
    defects: dict[str, str] = field(default_factory=lambda: dict(DEFECTS))

    # Which defect each question is the test for. The board derives "defects
    # neutralised" from the answers rather than from a keyword scan of prose,
    # so this mapping is what makes an answer attributable.
    probes: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "requests": ("duplicate-export", "malformed-lines"),
            "breach_day": ("field-rename", "unit-change"),
            "peak_hour": ("timezone-shift",),
        }
    )

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "breach_day": self.breach_day,
            "peak_hour": self.peak_hour,
            "naive": {
                "requests": self.naive_requests,
                "breach_day": self.naive_breach_day,
                "peak_hour": self.naive_peak_hour,
            },
            "rows_written": self.rows_written,
            "bytes_uncompressed": self.bytes_uncompressed,
            "malformed": self.malformed,
            "defects": self.defects,
            "probes": {k: list(v) for k, v in self.probes.items()},
        }


# --- generation ------------------------------------------------------------


def _hour_weights() -> list[float]:
    """A diurnal curve, peak-to-trough about 4:1, peaking at PEAK_HOUR_UTC."""
    return [
        1.0 + 3.0 * (0.5 + 0.5 * math.cos((hour - PEAK_HOUR_UTC) * math.pi / 12.0)) ** 3
        for hour in range(24)
    ]


def _pick(rng: random.Random, weights: list[float]) -> int:
    total = sum(weights)
    mark, running = rng.random() * total, 0.0
    for index, weight in enumerate(weights):
        running += weight
        if running >= mark:
            return index
    return len(weights) - 1


def _events(rng: random.Random, day: int, count: int) -> list[dict]:
    """One day of true events — real UTC timestamps, real milliseconds.

    Corruption happens on the way to disk, never here, so the same event list
    can be both written dirty and (in principle) reasoned about clean.
    """
    weights = _hour_weights()
    shares = [share for _, share, _, _ in ROUTES]
    events = []
    for index in range(count):
        route, _, median, spread = ROUTES[_pick(rng, shares)]
        hour = _pick(rng, weights)
        latency = rng.lognormvariate(math.log(median), spread)
        if route == "/checkout" and day >= REGRESSION_DAY:
            latency *= REGRESSION_FACTOR
        status = 200
        if rng.random() < 0.02:
            status = rng.choice((404, 500, 503))
        events.append(
            {
                "ts_hour": hour,
                "ts_min": rng.randrange(60),
                "ts_sec": rng.randrange(60),
                "ts_ms": rng.randrange(1000),
                "service": "edge",
                "route": route,
                "status": status,
                "latency_ms": round(latency, 1),
                "upstream": UPSTREAMS[route],
                "request_id": f"{day:02d}{index:07d}{rng.randrange(16**6):06x}",
                "bytes": rng.randrange(200, 24000),
            }
        )
    return events


def _render(event: dict, day: int) -> str:
    """One event as the line that actually lands on disk, defects applied."""
    hour = event["ts_hour"]
    if day >= TZ_SHIFT_DAY:
        # Local time wearing a UTC suffix. Not a formatting choice — this is the
        # defect, and it is invisible to anything that only parses the string.
        hour = (hour + TZ_OFFSET_HOURS) % 24
    stamp = (
        f"{YEAR}-{MONTH:02d}-{day:02d}T{hour:02d}:"
        f"{event['ts_min']:02d}:{event['ts_sec']:02d}.{event['ts_ms']:03d}Z"
    )
    latency = event["latency_ms"]
    if day >= UNITS_DAY:
        latency = round(latency * 1000)
    row = {
        "ts": stamp,
        "service": event["service"],
        ("path" if day >= RENAME_DAY else "route"): event["route"],
        "status": event["status"],
        "latency_ms": latency,
        "upstream": event["upstream"],
        "request_id": event["request_id"],
        "bytes": event["bytes"],
    }
    return json.dumps(row, separators=(",", ":"))


def generate(dest: Path, *, seed: int = 20260601, rows_per_day: int = 10_000) -> Truth:
    """Write the corpus under `dest/` and return the answer key.

    `rows_per_day` is the only size knob. Full size is ~300k rows / ~54MB
    uncompressed, comfortably past any context window; the offline tests run it
    small enough to be free. Truth is derived from the written bytes either way,
    so both sizes score by the same rules.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    rows_written = 0
    bytes_uncompressed = 0
    # Placed up front so the truncated lines land on chosen days rather than
    # wherever the per-day rng happened to be.
    malformed_at = sorted(
        (rng.randrange(1, DAYS + 1), rng.randrange(max(rows_per_day, 2)))
        for _ in range(MALFORMED_LINES)
    )

    for day in range(1, DAYS + 1):
        # A little day-to-day variation, so a reader cannot back out the row
        # count from the file count and skip the corpus entirely.
        count = max(2, int(rows_per_day * rng.uniform(0.85, 1.15)))
        events = _events(rng, day, count)
        lines = [_render(event, day) for event in events]

        for at_day, at_index in malformed_at:
            if at_day == day and at_index < len(lines):
                # A partial write: the tail of the line never made it to disk.
                lines.insert(at_index, lines[at_index][: rng.randrange(20, 60)])

        if day == DUPLICATE_DAY:
            # The export was retried and appended rather than replaced. Same
            # request_ids, so it is detectable — and invisible to a line count.
            lines = lines + lines

        payload = "".join(line + "\n" for line in lines)
        rows_written += len(lines)
        bytes_uncompressed += len(payload)
        path = dest / f"{YEAR}-{MONTH:02d}-{day:02d}.ndjson.gz"
        with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as handle:
            handle.write(payload.encode())

    return derive(
        dest, rows_written=rows_written, bytes_uncompressed=bytes_uncompressed
    )


# --- truth, read back off the artifact -------------------------------------


def _p95(values: list[float]) -> float:
    """Nearest-rank p95. The margins here are 50%+, so the choice of method
    cannot move an answer; it is fixed only so the number is reproducible."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def read_rows(dest: Path) -> tuple[list[dict], int]:
    """Every parseable row on disk with its day attached, and the count of lines
    that would not parse.

    This is the answer key's reader, so it is allowed to know that truncated
    lines exist. It returns the count rather than swallowing it because a
    corpus whose malformed lines failed to land would make one question free.
    """
    rows: list[dict] = []
    malformed = 0
    for path in sorted(Path(dest).glob("*.ndjson.gz")):
        day = int(path.name.split("-")[2].split(".")[0])
        with gzip.open(path, "rt") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                row["_day"] = day
                rows.append(row)
    return rows, malformed


def derive(dest: Path, *, rows_written: int = 0, bytes_uncompressed: int = 0) -> Truth:
    """Compute both the correct answers and the naive ones from the same bytes."""
    rows, malformed = read_rows(dest)

    # Q1 — requests. Correct: unique request_ids. Naive: parseable line count,
    # which double-counts the retried export.
    requests = len({row["request_id"] for row in rows})
    naive_requests = len(rows)

    # Q2 — breach day. Correct: honour the rename, undo the microseconds.
    # Naive: read `latency_ms` as written, which crosses the SLO the day the
    # units changed, six days before anything actually regressed.
    def checkout(row: dict) -> bool:
        return row.get("route", row.get("path")) == "/checkout"

    def latency(row: dict, *, fix_units: bool) -> float:
        value = float(row["latency_ms"])
        return value / 1000.0 if fix_units and row["_day"] >= UNITS_DAY else value

    seen: set[str] = set()
    by_day_true: dict[int, list[float]] = {}
    by_day_naive: dict[int, list[float]] = {}
    for row in rows:
        if not checkout(row):
            continue
        by_day_naive.setdefault(row["_day"], []).append(latency(row, fix_units=False))
        if row["request_id"] in seen:
            continue
        seen.add(row["request_id"])
        by_day_true.setdefault(row["_day"], []).append(latency(row, fix_units=True))

    def first_breach(by_day: dict[int, list[float]]) -> int:
        for day in sorted(by_day):
            if _p95(by_day[day]) > SLO_MS:
                return day
        return 0

    breach_day = first_breach(by_day_true)
    naive_breach_day = first_breach(by_day_naive)

    # Q3 — peak hour UTC. Correct: add back the offset on the shifted days.
    # Naive: trust the Z.
    hours_true = [0] * 24
    hours_naive = [0] * 24
    for row in rows:
        stated = int(row["ts"][11:13])
        hours_naive[stated] += 1
        shift = -TZ_OFFSET_HOURS if row["_day"] >= TZ_SHIFT_DAY else 0
        hours_true[(stated + shift) % 24] += 1

    return Truth(
        requests=requests,
        breach_day=breach_day,
        peak_hour=hours_true.index(max(hours_true)),
        naive_requests=naive_requests,
        naive_breach_day=naive_breach_day,
        naive_peak_hour=hours_naive.index(max(hours_naive)),
        rows_written=rows_written or len(rows),
        bytes_uncompressed=bytes_uncompressed,
        malformed=malformed,
    )


def verify(truth: Truth) -> None:
    """Assert the corpus still discriminates.

    Every one of these is a way the suite could quietly stop measuring anything:
    if the naive answer equals the correct one, an arm that never looked at the
    data scores the same as one that did, and the board keeps printing green.
    Run at full size, where sampling noise cannot explain a failure.
    """
    assert truth.breach_day == REGRESSION_DAY, (
        f"the real regression should surface on day {REGRESSION_DAY}, "
        f"got {truth.breach_day}"
    )
    assert truth.naive_breach_day == UNITS_DAY, (
        f"the unit change should be the naive answer (day {UNITS_DAY}), "
        f"got {truth.naive_breach_day}"
    )
    assert truth.peak_hour == PEAK_HOUR_UTC, (
        f"true peak should be {PEAK_HOUR_UTC}:00 UTC, got {truth.peak_hour}"
    )
    assert truth.naive_peak_hour != truth.peak_hour, (
        "the timezone defect changes no answer: the peak-hour question is "
        "measuring nothing"
    )
    assert truth.naive_requests > truth.requests, (
        "the duplicate export changes no answer: the request-count question is "
        "measuring nothing"
    )
    assert truth.malformed > 0, (
        "no unparseable lines landed: a naive parser would never have to recover"
    )
