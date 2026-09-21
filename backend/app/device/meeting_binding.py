"""Process-local device to active-meeting bindings for the MVP."""

from __future__ import annotations

from threading import RLock

from sqlalchemy.orm import Session

from app.common.exceptions import InvalidMeetingStateTransition
from app.meeting.meeting_model import Meeting

ACTIVE_MEETING_STATUS = "IN_PROGRESS"


class DeviceMeetingBindingConflict(InvalidMeetingStateTransition):
    """A device cannot be reassigned while its current meeting is active."""


class DeviceMeetingBindingRegistry:
    """Thread-safe, process-local device binding registry.

    The registry only owns the mapping. Meeting-state checks stay in the small
    service helpers below so callers can use their own short-lived DB session.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, str] = {}
        self._lock = RLock()

    def bind(self, device_id: str, meeting_id: str) -> bool:
        """Bind an unbound device; binding the same pair is idempotent."""
        with self._lock:
            current = self._bindings.get(device_id)
            if current == meeting_id:
                return False
            if current is not None:
                raise DeviceMeetingBindingConflict(
                    f"device {device_id} is already bound to meeting {current}"
                )
            self._bindings[device_id] = meeting_id
            return True

    def get(self, device_id: str) -> str | None:
        with self._lock:
            return self._bindings.get(device_id)

    def unbind(self, device_id: str, meeting_id: str | None = None) -> bool:
        """Remove a binding, optionally only when it still targets meeting_id."""
        with self._lock:
            current = self._bindings.get(device_id)
            if current is None or (meeting_id is not None and current != meeting_id):
                return False
            del self._bindings[device_id]
            return True

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()


device_meeting_bindings = DeviceMeetingBindingRegistry()


def bind_device_to_meeting(db: Session, device_id: str, meeting_id: str) -> None:
    """Bind a device, replacing only a stale non-active binding."""
    current = device_meeting_bindings.get(device_id)
    if current is not None and current != meeting_id:
        current_meeting = db.get(Meeting, current)
        if (
            current_meeting is not None
            and current_meeting.status == ACTIVE_MEETING_STATUS
        ):
            raise DeviceMeetingBindingConflict(
                f"device {device_id} is already bound to active meeting {current}"
            )
        device_meeting_bindings.unbind(device_id, current)

    device_meeting_bindings.bind(device_id, meeting_id)


def get_active_meeting(db: Session, device_id: str) -> str | None:
    """Resolve the device's meeting only while it is truly active."""
    meeting_id = device_meeting_bindings.get(device_id)
    if meeting_id is None:
        return None
    meeting = db.get(Meeting, meeting_id)
    if meeting is None or meeting.status != ACTIVE_MEETING_STATUS:
        return None
    return meeting_id


__all__ = [
    "ACTIVE_MEETING_STATUS",
    "DeviceMeetingBindingConflict",
    "DeviceMeetingBindingRegistry",
    "bind_device_to_meeting",
    "device_meeting_bindings",
    "get_active_meeting",
]
