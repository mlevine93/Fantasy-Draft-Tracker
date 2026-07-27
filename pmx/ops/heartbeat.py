"""Heartbeat and dead-man's switch.

§8: a silently dead bot with open positions is worse than no bot. A crashed process
stops trading, which sounds safe, but it also stops cancelling, stops reconciling, and
stops noticing that a resting order just became a losing position.

The switch is deliberately dumb: the loop writes a timestamp to a file, and anything —
another process, a cron job, a human — can read that file and see how long it has been.
No network, no daemon, no dependency that can fail in the same way the main loop failed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from pmx.core.clock import utc_now

__all__ = ["Heartbeat", "HeartbeatStatus"]


class HeartbeatStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    last_beat: datetime | None
    age: timedelta | None
    alive: bool
    detail: str


class Heartbeat:
    """A timestamp in a file, written by the main loop and read by anything."""

    def __init__(self, path: Path | str, *, max_silence: timedelta) -> None:
        self.path = Path(path)
        self.max_silence = max_silence

    def beat(self, note: str = "") -> datetime:
        """Record liveness. Called once per main-loop iteration."""
        now = utc_now()
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a reader never sees a half-written timestamp and
        # concludes the process is dead when it is merely mid-write.
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(f"{now.isoformat()}\n{note}\n")
        temporary.replace(self.path)
        return now

    def status(self, *, now: datetime | None = None) -> HeartbeatStatus:
        """How long since the last beat, and whether that is acceptable."""
        moment = now or utc_now()
        if not self.path.is_file():
            return HeartbeatStatus(
                last_beat=None,
                age=None,
                alive=False,
                detail=f"no heartbeat file at {self.path}: the loop has never run, or the "
                "working directory is wrong",
            )

        raw = self.path.read_text().splitlines()
        if not raw:
            return HeartbeatStatus(
                last_beat=None, age=None, alive=False, detail="heartbeat file is empty"
            )

        try:
            last = datetime.fromisoformat(raw[0].strip())
        except ValueError:
            return HeartbeatStatus(
                last_beat=None,
                age=None,
                alive=False,
                detail=f"heartbeat file is unreadable: {raw[0]!r}",
            )

        age = moment - last
        if age > self.max_silence:
            return HeartbeatStatus(
                last_beat=last,
                age=age,
                alive=False,
                detail=(
                    f"last heartbeat {age.total_seconds():.0f}s ago exceeds "
                    f"{self.max_silence.total_seconds():.0f}s: the loop is dead or wedged. "
                    "Check for open orders before restarting."
                ),
            )

        return HeartbeatStatus(
            last_beat=last,
            age=age,
            alive=True,
            detail=f"alive, last beat {age.total_seconds():.1f}s ago",
        )
