#!/usr/bin/env python3
"""Concurrent loop runner for the lifedraft pi chains.

Runs the project's long-running chains concurrently on independent intervals:

  - design-principles-review-pipeline (dp)
  - issue-pipeline                     (issue)
  - jurisdiction-audit                 (juris)

Each loop invokes `pi -p /run-chain <chain> -- <task>` on its own interval and
is gated by a shared semaphore so only one chain runs at a time (pi uses a
single global session). Use the per-chain `--<prefix>-interval`, `--<prefix>-provider`,
and `--<prefix>-model` flags to tune each loop independently, or `--no-juris`
/ `--no-dp` / `--no-issue` to disable a loop entirely.
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)


@dataclass
class Loop:
    name: str
    cmd: str
    interval: int
    provider: str | None = None
    model: str | None = None
    runs: int = 0
    last_run: float = 0
    last_dur: float = 0
    last_code: int = 0


@dataclass
class Stats:
    start: float = field(default_factory=time.time)
    counts: dict = field(default_factory=dict)
    errs: dict = field(default_factory=dict)

    def record(self, name: str, code: int) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1
        if code != 0:
            self.errs[name] = self.errs.get(name, 0) + 1


def build_cmd(base: str, p: str | None, m: str | None) -> list[str]:
    c = ["pi", "-p"]
    if p:
        c += ["--provider", p]
    if m:
        c += ["--model", m]
    c.append(base)
    return c


async def run(cmd: list[str], name: str, sem: asyncio.Semaphore) -> tuple[int, float]:
    t0 = time.time()
    log.info(f"[{name}] Starting: {' '.join(cmd)}")
    try:
        async with sem:
            proc = await asyncio.create_subprocess_exec(*cmd)
            code = await proc.wait()
        dt = time.time() - t0
        if code == 0:
            log.info(f"[{name}] Done in {dt:.1f}s")
        else:
            log.warning(f"[{name}] Failed in {dt:.1f}s (exit={code})")
        return code, dt
    except Exception as e:
        log.error(f"[{name}] Error: {e}")
        return -1, time.time() - t0


async def loop_run(lp: Loop, st: Stats, stop: asyncio.Event, sem: asyncio.Semaphore):
    while not stop.is_set():
        t0 = time.time()
        code, dur = await run(build_cmd(lp.cmd, lp.provider, lp.model), lp.name, sem)
        lp.runs += 1
        lp.last_run = t0
        lp.last_dur = dur
        lp.last_code = code
        st.record(lp.name, code)
        if lp.runs % 5 == 0:
            log.info(
                f"[{lp.name}] runs={lp.runs} last={lp.last_dur:.1f}s "
                f"exit={lp.last_code} uptime={time.time() - st.start:.0f}s"
            )
        if lp.interval > 0:
            try:
                await asyncio.wait_for(stop.wait(), lp.interval)
            except asyncio.TimeoutError:
                pass


async def stats_log(st: Stats, loops: list[Loop], stop: asyncio.Event):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), 60)
        except asyncio.TimeoutError:
            pass
        up = time.time() - st.start
        parts = " | ".join(
            f"{n}: runs={st.counts.get(n, 0)} err={st.errs.get(n, 0)}" for n in st.counts
        )
        log.info(f"[GLOBAL] up={up:.0f}s {parts}")
        for lp in loops:
            if lp.last_run:
                log.info(
                    f"  [{lp.name}] runs={lp.runs} ago={time.time() - lp.last_run:.0f}s "
                    f"dur={lp.last_dur:.1f}s exit={lp.last_code}"
                )


# Chain definitions: (prefix, chain name, default task, default interval)
CHAINS = [
    (
        "dp",
        "design-principles-review-pipeline",
        "Review the codebase main branch for design principle violations, "
        "find gaps against existing issues, and create issues for new violations.",
        1800,
    ),
    (
        "issue",
        "issue-pipeline",
        "Analyze all open issues, implement the top 5, review, and merge passing ones.",
        10,
    ),
    (
        "juris",
        "jurisdiction-audit",
        "Scan the codebase for all jurisdictions and programs, tag existing issues, "
        "research official rules, and create issues for coverage gaps.",
        3600,
    ),
]


async def main():
    ap = argparse.ArgumentParser(description="Run lifedraft pi chains concurrently")
    ap.add_argument("--provider", default=None, help="Default provider for all chains")
    ap.add_argument("--model", default=None, help="Default model for all chains")
    for prefix, chain, _, dinterval in CHAINS:
        ap.add_argument(f"--{prefix}-provider", default=None, help=f"Provider override for {chain} (falls back to --provider)")
        ap.add_argument(f"--{prefix}-model", default=None, help=f"Model override for {chain} (falls back to --model)")
        ap.add_argument(f"--{prefix}-interval", type=int, default=dinterval)
        ap.add_argument(f"--no-{prefix}", action="store_true", help=f"Disable the {chain} loop")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument(
        "--top-issues",
        type=int,
        default=5,
        help="Number of top issues for issue-pipeline (default: 5)",
    )
    a = ap.parse_args()
    logging.getLogger().setLevel(a.log_level)

    loops: list[Loop] = []
    for prefix, chain, default_task, _ in CHAINS:
        if getattr(a, f"no_{prefix}"):
            continue
        provider = getattr(a, f"{prefix}_provider") or a.provider
        model = getattr(a, f"{prefix}_model") or a.model
        interval = getattr(a, f"{prefix}_interval")
        task = default_task
        if prefix == "issue":
            task = f"Analyze all open issues, implement the top {a.top_issues}, review, and merge passing ones."
        loops.append(Loop(prefix, f"/run-chain {chain} -- {task}", interval, provider, model))

    if not loops:
        log.error("All loops disabled; nothing to run.")
        sys.exit(1)

    st = Stats()
    stop = asyncio.Event()
    sem = asyncio.Semaphore(1)
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    summary = " | ".join(
        f"{lp.name} every {lp.interval}s ({lp.provider or '(default)'}/{lp.model or '(default)'})"
        for lp in loops
    )
    log.info(f"Starting: {summary}")
    try:
        await asyncio.gather(
            *(loop_run(lp, st, stop, sem) for lp in loops),
            stats_log(st, loops, stop),
        )
    finally:
        up = time.time() - st.start
        parts = " ".join(f"{n}={st.counts.get(n, 0)}/{st.errs.get(n, 0)}" for n in st.counts)
        log.info(f"=== Done: up={up:.0f}s {parts} ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
