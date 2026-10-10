"""Numbers about the gateway's work, in the format Prometheus collects.

Each gateway replica counts what it handled itself and serves the totals at
/metrics; Prometheus scrapes every replica and adds them up. The figures in the
dashboard come from the audit log instead, which all replicas share.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from gateway.upstreams import UpstreamState


class Metrics:
    def __init__(self) -> None:
        # A registry of its own, rather than the library's global one, so that
        # two apps in one process (as in the tests) do not share counters.
        self.registry = CollectorRegistry()
        self.calls = Counter(
            "gateway_tool_calls_total",
            "Tool calls handled, by upstream server, outcome and where the call came from.",
            ["server", "outcome", "source"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "gateway_tool_call_duration_seconds",
            "How long tool calls took, by upstream server.",
            ["server"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
            registry=self.registry,
        )
        self.upstream_up = Gauge(
            "gateway_upstream_up",
            "1 if this replica's last check of the upstream server succeeded, else 0.",
            ["server"],
            registry=self.registry,
        )
        self.upstream_tools = Gauge(
            "gateway_upstream_tools", "Tools the upstream server exposes.", ["server"], registry=self.registry
        )
        self.upstream_probe = Gauge(
            "gateway_upstream_probe_duration_seconds",
            "How long the last successful check of the upstream server took.",
            ["server"],
            registry=self.registry,
        )

    def observe_call(self, server: str, outcome: str, source: str, seconds: float) -> None:
        self.calls.labels(server, outcome, source).inc()
        self.duration.labels(server).observe(seconds)

    def render(self, states: list[UpstreamState]) -> bytes:
        """The current figures as text. Upstream health is read at this moment."""
        for gauge in (self.upstream_up, self.upstream_tools, self.upstream_probe):
            gauge.clear()
        for state in states:
            if not state.server.enabled:
                continue
            slug = state.server.slug
            self.upstream_up.labels(slug).set(1 if state.status == "connected" else 0)
            self.upstream_tools.labels(slug).set(len(state.tools))
            if state.latency_ms is not None:
                self.upstream_probe.labels(slug).set(state.latency_ms / 1000)
        return generate_latest(self.registry)
