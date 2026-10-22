from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import struct
import sys
import threading
import time
from typing import Any


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x0100
PAGE_NOACCESS = 0x0001
MAX_FAST_ADDRESS = 0x10000000000
MAX_REGION_SIZE = 2 * 1024 * 1024
ANCHOR = struct.pack("<f", 0.1)
ROW = struct.Struct("<10f")


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _valid_row(values: tuple[float, ...]) -> bool:
    if len(values) != 10 or not all(math.isfinite(value) for value in values):
        return False
    x, y, z, scale, width, height, dx, dy, dz, direction_length = values
    magnitude = math.sqrt(dx * dx + dy * dy + dz * dz)
    return (
        -100000.0 < x < 100000.0
        and -100000.0 < y < 100000.0
        and -10000.0 < z < 10000.0
        and abs(scale - 0.1) < 1e-6
        and 640.0 <= width <= 8192.0
        and 480.0 <= height <= 8192.0
        and abs(width - round(width)) < 0.001
        and abs(height - round(height)) < 0.001
        and 0.98 <= magnitude <= 1.02
        and 0.98 <= direction_length <= 1.02
    )


class ScrapMechanicLiveTracker:
    """Read the local player's live transform without modifying the game.

    Scrap Mechanic keeps a small view-state record containing world position,
    render size, and the normalized camera direction.  The record is found
    from its shape instead of a build-specific absolute address, so ASLR and
    game updates do not turn this into a hard-coded pointer hack.  After the
    one-time bounded scan, each sample is one 40-byte ReadProcessMemory call.
    """

    def __init__(self) -> None:
        self._kernel32: Any = None
        self._handle: int | None = None
        self._pid = 0
        self._address = 0
        self._candidates: list[int] = []
        self._candidate_values: dict[int, tuple[float, ...]] = {}
        self._candidate_motion_streaks: dict[int, int] = {}
        self._candidate_probe_cursor = 0
        self._selected_quiet_since = 0.0
        self._selected_motion_samples = 0
        self._selected_rejected_samples = 0
        self._last_accepted_values: tuple[float, ...] | None = None
        self._last_candidate_probe = 0.0
        self._last_scan = 0.0
        self._lock = threading.RLock()
        if sys.platform == "win32":
            self._configure_windows_api()

    def _configure_windows_api(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.VirtualQueryEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(MEMORY_BASIC_INFORMATION),
            ctypes.c_size_t,
        ]
        kernel32.VirtualQueryEx.restype = ctypes.c_size_t
        kernel32.ReadProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.ReadProcessMemory.restype = wintypes.BOOL
        self._kernel32 = kernel32

    def close(self) -> None:
        with self._lock:
            if self._handle and self._kernel32 is not None:
                self._kernel32.CloseHandle(self._handle)
            self._handle = None
            self._pid = 0
            self._address = 0
            self._candidates = []
            self._candidate_values = {}
            self._candidate_motion_streaks = {}
            self._candidate_probe_cursor = 0
            self._selected_quiet_since = 0.0
            self._selected_motion_samples = 0
            self._selected_rejected_samples = 0
            self._last_accepted_values = None
            self._last_candidate_probe = 0.0

    def _attach(self, pid: int) -> bool:
        if sys.platform != "win32" or self._kernel32 is None or pid <= 0:
            return False
        if self._handle and self._pid == pid:
            return True
        self.close()
        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
        )
        if not handle:
            return False
        self._handle = handle
        self._pid = pid
        return True

    def _read_bytes(self, address: int, size: int) -> bytes | None:
        if not self._handle or self._kernel32 is None:
            return None
        buffer = ctypes.create_string_buffer(size)
        received = ctypes.c_size_t()
        if not self._kernel32.ReadProcessMemory(
            self._handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(received),
        ):
            return None
        if received.value != size:
            return None
        return buffer.raw

    def _read_row(self, address: int) -> tuple[float, ...] | None:
        data = self._read_bytes(address, ROW.size)
        if data is None:
            return None
        values = ROW.unpack(data)
        return values if _valid_row(values) else None

    @staticmethod
    def _distance(values: tuple[float, ...], expected: tuple[float, float, float] | None) -> float:
        x, y, z = values[:3]
        # Several initialized renderer records have the same shape but remain
        # parked at the origin.  They are never the live player, even when an
        # old save snapshot happens to be closer to 0,0,0 than the player is.
        if abs(x) + abs(y) + abs(z) <= 1.0:
            return float("inf")
        if expected is None:
            return 0.0
        return math.dist((x, y, z), expected)

    def _scan_range(
        self,
        expected: tuple[float, float, float] | None,
        address_limit: int | None,
    ) -> list[tuple[float, int, tuple[float, ...]]]:
        if not self._handle or self._kernel32 is None:
            return None
        address = 0
        info = MEMORY_BASIC_INFORMATION()
        matches: list[tuple[float, int, tuple[float, ...]]] = []
        while self._kernel32.VirtualQueryEx(
            self._handle,
            ctypes.c_void_p(address),
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            base = int(info.BaseAddress or 0)
            size = int(info.RegionSize)
            if address_limit is not None and base >= address_limit:
                break
            readable = (
                info.State == MEM_COMMIT
                and info.Type == MEM_PRIVATE
                and not (info.Protect & (PAGE_GUARD | PAGE_NOACCESS))
                and ROW.size <= size <= MAX_REGION_SIZE
            )
            if readable:
                data = self._read_bytes(base, size)
                if data:
                    offset = 0
                    while True:
                        anchor = data.find(ANCHOR, offset)
                        if anchor < 0:
                            break
                        start = anchor - 12
                        if start >= 0 and start % 4 == 0 and start + ROW.size <= len(data):
                            values = ROW.unpack_from(data, start)
                            if _valid_row(values):
                                distance = self._distance(values, expected)
                                if math.isfinite(distance):
                                    matches.append((distance, base + start, values))
                        offset = anchor + 1
            next_address = base + max(size, 0x1000)
            if next_address <= address:
                break
            address = next_address
        return sorted(matches, key=lambda item: (item[0], item[1]))

    def _scan(
        self, expected: tuple[float, float, float] | None
    ) -> tuple[int, tuple[float, ...]] | None:
        # The live camera/view allocation is normally in the low private arena.
        # This fast pass reads about 20 MiB on the current game build.  Retain a
        # full-address fallback for future allocators rather than assuming the
        # observed address layout will never change.
        matches = self._scan_range(expected, MAX_FAST_ADDRESS)
        if not matches:
            matches = self._scan_range(expected, None)
        if not matches:
            return None

        # The engine retains several byte-for-byte copies of this view-state
        # structure.  Some are snapshots updated only during autosave or
        # renderer bookkeeping.  Keep the plausible addresses so a short,
        # cheap temporal probe can identify the copy that changes every frame
        # when the player starts moving.  Picking the lowest address produced
        # the former five-second, camera-dependent minimap lag.
        best_distance = matches[0][0]
        nearby = [
            item
            for item in matches
            if item[0] <= best_distance + 2048.0
        ][:256]
        self._candidates = [address for _distance, address, _values in nearby]
        self._candidate_values = {
            address: values for _distance, address, values in nearby
        }
        self._candidate_motion_streaks = {address: 0 for address in self._candidates}
        self._candidate_probe_cursor = 0
        self._selected_quiet_since = time.monotonic()
        self._selected_motion_samples = 0
        self._selected_rejected_samples = 0
        _distance, found_address, found_values = nearby[0]
        self._last_accepted_values = found_values
        return found_address, found_values

    @staticmethod
    def _row_delta(before: tuple[float, ...], after: tuple[float, ...]) -> float:
        position = math.dist(before[:3], after[:3])
        direction = math.dist(before[6:9], after[6:9])
        return position + direction * 16.0

    def _probe_candidates(self, selected: tuple[float, ...]) -> tuple[float, ...]:
        """Select the per-frame record among otherwise identical snapshots."""
        now = time.monotonic()
        accepted = self._last_accepted_values
        if accepted is not None:
            position_step = math.dist(accepted[:3], selected[:3])
            step = self._row_delta(accepted, selected)
            trusted = len(self._candidates) < 2 or self._selected_motion_samples >= 2
            plausible = position_step <= 16.0 and step <= 24.0
            if trusted and plausible:
                if step > 1e-5:
                    # Once an allocation has proved that it changes with the
                    # live camera, the row already read by ``read`` is all we
                    # need.  Re-reading up to 256 snapshot copies every 50 ms
                    # caused periodic source stalls even though the selected
                    # 40-byte record itself was current.
                    self._selected_motion_samples = min(
                        120, self._selected_motion_samples + 1
                    )
                    self._selected_quiet_since = now
                    self._selected_rejected_samples = 0
                    self._last_accepted_values = selected
                    if self._address:
                        self._candidate_values[self._address] = selected
                    return selected
                # While the player and camera are genuinely still, begin a
                # *sliced* alternate-record audit after a short grace period.
                # Only a small rotating batch is read on each sampler tick;
                # the old all-at-once sweep of up to 256 records was the
                # repeatable source of the minimap's occasional hard pause.
                quiet_for = now - self._selected_quiet_since
                if quiet_for < 0.10 or now - self._last_candidate_probe < 0.012:
                    return accepted
        if now - self._last_candidate_probe < 0.012:
            if accepted is None:
                self._last_accepted_values = selected
                return selected
            # Candidate enumeration remains throttled because it may inspect
            # hundreds of renderer snapshots. Once the selected allocation is
            # trusted, however, the caller has already read its current
            # 40-byte row; accepting that plausible row here preserves the
            # game's real 60 Hz movement/turning instead of repeating a 20 Hz
            # value until the next expensive candidate sweep.
            position_step = math.dist(accepted[:3], selected[:3])
            step = self._row_delta(accepted, selected)
            trusted = len(self._candidates) < 2 or self._selected_motion_samples >= 2
            plausible = position_step <= 16.0 and step <= 24.0
            if trusted and plausible:
                if step > 1e-5:
                    self._selected_motion_samples = min(
                        120, self._selected_motion_samples + 1
                    )
                    self._selected_quiet_since = now
                self._selected_rejected_samples = 0
                self._last_accepted_values = selected
                return selected
            return accepted
        self._last_candidate_probe = now
        if len(self._candidates) < 2:
            # Some game sessions expose only the real view-state allocation.
            # Returning ``_last_accepted_values`` unconditionally in that case
            # froze the minimap at its first sample.  Still apply the rollback
            # guard, but allow the sole live record to advance every probe.
            accepted = self._last_accepted_values
            if accepted is None:
                self._last_accepted_values = selected
                return selected
            position_step = math.dist(accepted[:3], selected[:3])
            step = self._row_delta(accepted, selected)
            trusted = self._selected_motion_samples >= 3
            plausible = position_step <= 16.0 and step <= 24.0
            if trusted and not plausible:
                # Autosaves can replay an old renderer snapshot at this same
                # address for several seconds.  A full process-memory rescan
                # here made the otherwise smooth minimap stop every autosave.
                # Keep the proven live address and last continuous transform;
                # ``read`` still rescans if the allocation actually vanishes.
                self._selected_rejected_samples = min(
                    1200, self._selected_rejected_samples + 1
                )
                return accepted
            self._selected_rejected_samples = 0
            if step > 1e-5:
                self._selected_motion_samples = min(
                    120, self._selected_motion_samples + 1
                )
                self._selected_quiet_since = now
            self._last_accepted_values = selected
            return selected
        changed: list[tuple[float, int, tuple[float, ...]]] = []
        # Audit dormant renderer copies incrementally.  ReadProcessMemory has
        # enough per-call overhead on the user's current game build that a
        # 32-row slice still took ~31 ms and silently reduced the supposedly
        # 60 Hz source to ~32 Hz whenever the player stood still.  Eight rows
        # fit comfortably inside the 16.7 ms sampler budget.  If the proven
        # selected row has actually gone stale, temporarily use sixteen so a
        # replacement is found faster; that recovery path is rare and bounded.
        # Keep values for rows outside this slice so their temporal motion
        # streak survives until the next rotation.
        current_values: dict[int, tuple[float, ...]] = dict(self._candidate_values)
        candidate_count = len(self._candidates)
        selected_is_stale = bool(
            accepted is not None
            and self._selected_motion_samples >= 3
            and (
                math.dist(accepted[:3], selected[:3]) > 16.0
                or self._row_delta(accepted, selected) > 24.0
            )
        )
        if selected_is_stale:
            requested_batch = 16
        elif self._selected_motion_samples >= 2:
            # Once this allocation has demonstrated continuous movement, a
            # quiet audit is only a low-priority guard against an engine-side
            # record migration.  Two rows keep that guard alive without
            # creating a visible hitch when movement resumes.
            requested_batch = 2
        else:
            # During startup, rotate quickly enough to identify which of the
            # look-alike renderer rows is the live transform.
            requested_batch = 8
        batch_size = min(requested_batch, candidate_count)
        start = self._candidate_probe_cursor % candidate_count
        probe_addresses = [
            self._candidates[(start + offset) % candidate_count]
            for offset in range(batch_size)
        ]
        self._candidate_probe_cursor = (start + batch_size) % candidate_count
        # Keep the selected row in every slice so selected-change logic stays
        # immediate even when its address is outside the rotating batch.
        if self._address not in probe_addresses:
            probe_addresses.append(self._address)
        for address in probe_addresses:
            values = self._read_row(address)
            if values is None:
                continue
            before = self._candidate_values.get(address)
            current_values[address] = values
            if before is not None:
                delta = self._row_delta(before, values)
                if delta > 1e-5:
                    changed.append((delta, address, values))
                    self._candidate_motion_streaks[address] = min(
                        8, self._candidate_motion_streaks.get(address, 0) + 1
                    )
                else:
                    self._candidate_motion_streaks[address] = 0
        self._candidate_values = current_values
        selected_change = next(
            (item for item in changed if item[1] == self._address), None
        )
        accepted = self._last_accepted_values
        if (
            selected_change is None
            and accepted is not None
            and self._selected_motion_samples >= 3
            and (
                math.dist(accepted[:3], selected[:3]) > 16.0
                or self._row_delta(accepted, selected) > 24.0
            )
        ):
            # The first stale copy update was rejected on the previous probe.
            # It is now stationary, so it no longer appears in ``changed``;
            # nevertheless the selected address still returns the bad old
            # row. Keep rejecting it and eventually rescan around the last
            # accepted position instead of silently adopting it here.
            alternatives = [
                item for item in changed
                if item[1] != self._address
                and self._candidate_motion_streaks.get(item[1], 0) >= 2
                and math.dist(item[2][:3], accepted[:3]) <= 16.0
                and self._row_delta(accepted, item[2]) <= 24.0
            ]
            if alternatives:
                # A continuously changing, nearby alternate is the new live
                # frame record. Switch immediately instead of remaining
                # frozen forever on the selected autosave copy.
                _delta, self._address, candidate = min(
                    alternatives,
                    key=lambda item: math.dist(item[2][:3], accepted[:3]),
                )
                self._selected_rejected_samples = 0
                self._selected_quiet_since = now
                self._selected_motion_samples = 2
                self._last_accepted_values = candidate
                return candidate
            self._selected_rejected_samples = min(
                1200, self._selected_rejected_samples + 1
            )
            return accepted
        if selected_change is not None:
            # Once the per-frame record is selected, keep it while it is
            # moving.  Autosaves update old renderer snapshots in one large
            # jump; selecting the largest delta made the minimap teleport back
            # to that old position for several samples.
            candidate = selected_change[2]
            accepted = self._last_accepted_values or selected
            step = self._row_delta(accepted, candidate)
            position_step = math.dist(accepted[:3], candidate[:3])

            # A genuine per-frame player record moves continuously. Snapshot
            # copies jump to an older autosave position in one update and can
            # remain there for several probes. Once a record has proved live,
            # reject an isolated implausible jump instead of letting it pull
            # the minimap backwards for several seconds.
            trusted = self._selected_motion_samples >= 3
            # At the 20 Hz memory sampling cadence even a fast vehicle cannot
            # legitimately cover dozens of metres between samples. Keep a
            # generous allowance for lag spikes.  Crucially, never age an
            # implausible record into being trusted: one of the engine's
            # renderer copies can keep replaying an old autosave while the
            # real player is still moving, which formerly pulled the marker
            # backwards after the one-second rejection timeout.
            plausible = position_step <= 16.0 and step <= 24.0
            if trusted and not plausible:
                self._selected_rejected_samples += 1
                alternatives = [
                    item for item in changed
                    if item[1] != self._address
                    and math.dist(item[2][:3], accepted[:3]) <= 16.0
                    and self._candidate_motion_streaks.get(item[1], 0) >= 2
                ]
                if alternatives:
                    _delta, self._address, candidate = min(
                        alternatives,
                        key=lambda item: math.dist(item[2][:3], accepted[:3]),
                    )
                    self._selected_rejected_samples = 0
                else:
                    # Keep the last continuous position without initiating a
                    # costly scan.  The selected allocation is discarded only
                    # by the invalid/disappeared-record paths below.
                    return accepted
            else:
                self._selected_rejected_samples = 0

            self._selected_quiet_since = now
            if self._row_delta(accepted, candidate) > 1e-5:
                self._selected_motion_samples = min(120, self._selected_motion_samples + 1)
            self._last_accepted_values = candidate
            return candidate

        if not self._selected_quiet_since:
            self._selected_quiet_since = now
        stable_changes = [
            item
            for item in changed
            if self._candidate_motion_streaks.get(item[1], 0) >= 3
        ]
        if stable_changes and now - self._selected_quiet_since >= 0.10:
            # A genuine live record changes on consecutive 20 Hz probes.  An
            # autosave copy normally changes once, so it never passes this
            # temporal test.  Prefer continuity over raw movement magnitude.
            _delta, self._address, selected = min(
                stable_changes,
                key=lambda item: math.dist(
                    item[2][:3],
                    (self._last_accepted_values or selected)[:3],
                ),
            )
            self._selected_quiet_since = now
            self._selected_motion_samples = 1
            self._selected_rejected_samples = 0
        elif self._address not in current_values:
            # The selected allocation disappeared. Do not preserve its stale
            # address forever; allow the next scan to rediscover the live row
            # around the last continuous position.
            self._address = 0
            return self._last_accepted_values or selected
        self._last_accepted_values = selected
        return selected

    def read(
        self,
        pid: int,
        expected: tuple[float, float, float] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            if not self._attach(pid):
                return None
            values = self._read_row(self._address) if self._address else None
            if values is None:
                now = time.monotonic()
                if now - self._last_scan < 1.0:
                    return None
                self._last_scan = now
                scan_expected = (
                    tuple(self._last_accepted_values[:3])
                    if self._last_accepted_values is not None
                    else expected
                )
                found = self._scan(scan_expected)
                if found is None:
                    return None
                self._address, values = found
            values = self._probe_candidates(values)
            x, y, z, _scale, _width, _height, dx, dy, dz, _length = values
            return {
                "position": {"x": x, "y": y, "z": z},
                "direction": {"x": dx, "y": dy, "z": dz},
                "updated": time.time(),
            }
