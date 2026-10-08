import pytest

from repo2skill.extractors.parsing import parse_selected, parse_yaml


def test_yaml_base_loader_preserves_yaml_11_boolean_spellings():
    assert parse_yaml("on: yes\noff: no\n") == {"on": "yes", "off": "no"}


def test_yaml_rejects_recursive_aliases_before_constructing_objects():
    with pytest.raises(ValueError, match="recursive"):
        parse_yaml("loop: &loop\n  next: *loop\n")


def test_json_rejects_nonfinite_numbers():
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_selected('{"value": NaN}', "json")
