"""End-to-end request-id traceability (#24).

One incoming ``X-Request-ID`` must survive the whole pipeline:

    structured request log  ->  audit_event.request_id
                             ->  post_revision.request_id
                             ->  event_outbox.request_id
                             ->  webhook_deliveries.request_id
                             ->  W3C traceparent echoed to the subscriber

The webhook subscriber is a real local HTTP server so we capture the outbound
``traceparent`` header delivered over the wire.
"""

from __future__ import annotations

import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from app.models.audit_event import AuditEvent
from app.models.event_outbox import EventOutbox
from app.models.post import Post
from app.models.post_revision import PostRevision
from app.models.site import Site
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE_SLUG = "blog"


class _SubscriberServer:
    """Minimal local HTTP subscriber recording headers/bodies it receives."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        received_list = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                received_list.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self._httpd: HTTPServer = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _SubscriberServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=2.0)
        self._httpd.server_close()


class TestRequestIdEndToEnd:
    def test_request_id_propagates_through_audit_outbox_delivery(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        from app.services.webhook import create_webhook, dispatch_pending_events

        site = Site(id=uuid.uuid4(), slug=SITE_SLUG, name="Blog", publish_mode="auto")
        db.add(site)
        db.commit()

        request_id = f"e2e-{uuid.uuid4().hex[:16]}"

        with _SubscriberServer() as subscriber:
            webhook, _secret = create_webhook(
                db,
                url=f"http://127.0.0.1:{subscriber.port}/hooks/blog",
                event_types=["post.created", "post.published"],
                site_slug=SITE_SLUG,
            )

            resp = client.post(
                f"/v1/sites/{SITE_SLUG}/posts",
                json={"title": "Trace Me", "body_md": "# Trace\n\nbody."},
                headers={**auth_headers, "X-Request-ID": request_id},
            )
            assert resp.status_code == 201, resp.text

            # The response echoes the request id back.
            assert resp.headers.get("x-request-id") == request_id

            post = db.query(Post).first()
            assert post is not None

            # 1. audit_event carries the request id.
            audit = db.query(AuditEvent).filter(AuditEvent.request_id == request_id).first()
            assert audit is not None, "no audit event with the request id"
            assert audit.action == "post.created"

            # 2. post_revision carries it too.
            rev = db.query(PostRevision).filter(PostRevision.request_id == request_id).first()
            assert rev is not None and rev.post_id == post.id

            # 3. the outbox event carries it.
            outbox = (
                db.query(EventOutbox)
                .filter(EventOutbox.event_type == "post.created", EventOutbox.request_id == request_id)
                .first()
            )
            assert outbox is not None

            # 4. dispatch triggers a delivery carrying request_id + traceparent.
            dispatched = dispatch_pending_events(db)
            db.commit()
            assert dispatched >= 1

            from app.models.webhook import WebhookDelivery

            delivery = (
                db.query(WebhookDelivery)
                .filter(
                    WebhookDelivery.webhook_id == webhook.id,
                    WebhookDelivery.event == "post.created",
                )
                .first()
            )
            assert delivery is not None
            assert delivery.request_id == request_id

            # 5. the subscriber received a valid W3C traceparent over the wire.
            assert len(subscriber.received) >= 1
            tp = subscriber.received[0]["headers"].get("traceparent", "")
            parts = tp.split("-")
            assert len(parts) == 4 and parts[0] == "00", f"bad traceparent: {tp!r}"
            assert len(parts[2]) == 16  # span id hex

            # 6. redelivery replays with byte-identical payload + same request id.
            redeliver_url = f"/v1/admin/webhooks/{webhook.id}/redeliver/{delivery.id}"
            resp2 = client.post(redeliver_url, headers=auth_headers)
            assert resp2.status_code == 200, resp2.text

            from app.models.webhook import WebhookDelivery as WD2

            replayed = (
                db.query(WD2)
                .filter(
                    WD2.webhook_id == webhook.id,
                    WD2.event == "post.created",
                    WD2.id != delivery.id,
                )
                .order_by(WD2.created_at.desc())
                .first()
            )
            assert replayed is not None
            assert replayed.request_id == request_id
            assert replayed.payload == delivery.payload
