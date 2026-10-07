"""Tracing test: one agent request produces ONE trace with the expected span tree.

Runs in-process with an in-memory span exporter instead of Jaeger.
    pytest tests/test_tracing.py -v
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from test_coordinator import AGENT_URLS, build_platform
from coordinator.main import create_app as create_coordinator  # noqa: E402

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture
def traced_coordinator():
    network, customer_gw, order_gw = build_platform()
    app = create_coordinator(a2a_transport=network, agent_urls=AGENT_URLS)
    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    EXPORTER.clear()
    with TestClient(app) as client:
        yield client, order_gw


def spans_by_name():
    return {s.name: s for s in EXPORTER.get_finished_spans()}


def test_key_scenario_is_one_trace_with_the_expected_tree(traced_coordinator):
    client, _ = traced_coordinator
    r = client.post("/agent/query", headers={"X-Correlation-ID": "trace-test-1"},
                    json={"query": "Find customer C001 and tell me their latest order status"}).json()
    spans = spans_by_name()

    expected = {"POST /agent/query", "coordinator.plan", "delegate get_customer", "a2a.task get_customer",
                "delegate get_latest_order", "a2a.task get_latest_order", "coordinator.answer"}
    assert expected <= set(spans)
    assert len({s.context.trace_id for s in EXPORTER.get_finished_spans()}) == 1     # ONE trace
    assert r["trace_id"] == format(spans["POST /agent/query"].context.trace_id, "032x")

    root = spans["POST /agent/query"]
    assert root.attributes["correlation.id"] == "trace-test-1"
    assert root.attributes["agent.status"] == "completed"
    for skill in ("get_customer", "get_latest_order"):
        delegate, task = spans[f"delegate {skill}"], spans[f"a2a.task {skill}"]
        assert delegate.parent.span_id == root.context.span_id
        assert task.parent.span_id == delegate.context.span_id     # agent work nested under delegation
        assert task.attributes["a2a.outcome"] == "found"
    assert spans["coordinator.plan"].attributes["plan.steps"] == "s1:get_customer -> s2:get_latest_order"


def test_failures_are_marked_as_errors_with_retry_events(traced_coordinator):
    client, order_gw = traced_coordinator
    order_gw.override = lambda req: httpx.Response(503, json={"message": "down"})
    client.post("/agent/query", json={"query": "Find customer C001 and tell me their latest order status"})
    spans = spans_by_name()
    delegate, task = spans["delegate get_latest_order"], spans["a2a.task get_latest_order"]
    assert delegate.status.status_code == trace.StatusCode.ERROR
    assert delegate.attributes["error.code"] == "BACKEND_UNAVAILABLE"
    assert task.status.status_code == trace.StatusCode.ERROR
    assert "retry" in [e.name for e in task.events]
    assert spans["delegate get_customer"].status.status_code != trace.StatusCode.ERROR
