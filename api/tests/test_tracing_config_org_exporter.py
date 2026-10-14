"""Re-registering an org's Langfuse exporter must not depend on exporter internals.

register_org used to read ``existing._headers`` off the OTLP exporter to decide
whether credentials had changed. Newer opentelemetry releases dropped that
attribute, so every second registration for an org raised AttributeError and
broke post-call integrations.
"""

from api.services.pipecat.tracing_config import _OrgRoutingExporter


def _router():
    return _OrgRoutingExporter(default_exporter=object())


def test_registering_same_credentials_twice_is_a_noop():
    router = _router()
    router.register_org(1, "https://lf.example.com/", "pk", "sk")
    first = router._org_exporters["1"]

    router.register_org(1, "https://lf.example.com/", "pk", "sk")

    assert router._org_exporters["1"] is first


def test_changed_credentials_replace_the_exporter():
    router = _router()
    router.register_org(1, "https://lf.example.com", "pk", "sk")
    first = router._org_exporters["1"]

    router.register_org(1, "https://lf.example.com", "pk", "new-sk")

    assert router._org_exporters["1"] is not first


def test_changed_host_replaces_the_exporter():
    router = _router()
    router.register_org(1, "https://a.example.com", "pk", "sk")
    first = router._org_exporters["1"]

    router.register_org(1, "https://b.example.com", "pk", "sk")

    assert router._org_exporters["1"] is not first
    assert router.get_org_host(1) == "https://b.example.com"


def test_unregister_forgets_settings():
    router = _router()
    router.register_org(1, "https://lf.example.com", "pk", "sk")
    first = router._org_exporters["1"]

    router.unregister_org(1)
    router.register_org(1, "https://lf.example.com", "pk", "sk")

    assert router._org_exporters["1"] is not first
