"""Email and text alerts through the attendee's own Amazon SNS topic.

re:Seat publishes each alert once to one topic. The attendee subscribes an email address to
it, and later a phone number for text messages. The same words as the phone push.

Rules this module exists to respect:
- No AWS keys in re:Seat. boto3 finds credentials the usual way, from `aws_profile` in the
  rules file when set, else the default chain. Nothing about them is printed or stored.
- The Events API access token never goes to AWS SNS. Only the alert text does: session codes,
  titles, the event type, and a link to the web app when the phone can reach it.
- Publishing is best effort, on its own thread, so a slow or failing SNS never holds up the
  watcher. A failure is counted, never retried in a loop.
- Only alerts worth an email go: things to act on, and things that changed the schedule.
- US text messages need a registered origination number in the attendee's AWS account.
  That is set up in AWS, not here. Email works as soon as the subscription is confirmed.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

from . import push
from .watcher import WatchEvent

KINDS = ("offer", "new_sessions", "booked", "not_booked", "unconfirmed", "proposed", "swap", "removed",
         "moved", "signin", "offline", "rules")
SUBJECT_MAX = 100          # SNS rejects a longer email subject


def _client(profile: str | None, region: str) -> Any:
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError("Email and text alerts need boto3: pip install 'reseat[aws]'.") from e
    return boto3.session.Session(profile_name=profile).client("sns", region_name=region)


def region_of(topic_arn: str) -> str:
    """arn:aws:sns:<region>:<account>:<name>. The rules file pattern guarantees the shape."""
    return topic_arn.split(":")[3]


class SnsNotifier:
    """A watcher subscriber that publishes chosen alerts to one SNS topic on a background thread."""

    def __init__(self, topic_arn: str, code_title: Callable[[str], str] = str, click_base: str | None = None,
                 profile: str | None = None, client: Any = None):
        self.topic_arn, self.code_title, self.click_base = topic_arn, code_title, click_base
        self._profile = profile
        self._sns = client
        self.sent = 0
        self.failures = 0
        self.last_error: str | None = None
        self._q: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None

    def __call__(self, ev: WatchEvent) -> None:
        if ev.kind not in KINDS:
            return
        msg = push.message(ev, self.code_title)
        if not msg:
            return
        body = msg[1]
        if self.click_base:
            body += f"\n\nOpen re:Seat: {self.click_base}{push.page_for(ev)}"
        try:
            self._q.put_nowait((msg[0], body))
        except queue.Full:
            self.failures += 1

    def publish(self, title: str, body: str) -> bool:
        subject = push.CONTROL.sub(" ", f"re:Seat: {title}").encode("ascii", "replace").decode()
        try:
            if self._sns is None:
                self._sns = _client(self._profile, region_of(self.topic_arn))
            self._sns.publish(TopicArn=self.topic_arn, Subject=subject[:SUBJECT_MAX], Message=body)
        except Exception as e:  # noqa: BLE001  best effort: count it, keep the watcher going
            self.failures += 1
            self.last_error = type(e).__name__        # never the message: it can name the account
            return False
        self.sent += 1
        return True

    def start(self) -> None:
        def loop() -> None:
            while True:
                item = self._q.get()
                if item is None:
                    return
                self.publish(*item)
        self._thread = threading.Thread(target=loop, name="reseat-sns", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def info(self) -> dict[str, Any]:
        return {"sent": self.sent, "failures": self.failures, "last_error": self.last_error}
