from repo2skill.extractors.base import Extractor, ExtractionSession
from repo2skill.extractors.registry import discover_extractors


class _Extractor:
    extractor_id = "z-test"
    version = "1"

    def discover(self, ctx):
        return ()

    def extract(self, ctx, selectors):
        raise AssertionError("no rules")


def test_registry_orders_extractors_by_id_and_version():
    assert [item.extractor_id for item in discover_extractors([_Extractor()])] == ["z-test"]
