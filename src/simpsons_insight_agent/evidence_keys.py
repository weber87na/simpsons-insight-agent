"""Shared opaque evidence identifiers for structured model calls."""


def map_evidence_ids(value, mapping, field=""):
    if isinstance(value, dict):
        return {k: map_evidence_ids(v, mapping, k) for k, v in value.items()}
    if isinstance(value, list):
        return [map_evidence_ids(v, mapping, field) for v in value]
    if isinstance(value, str) and (field == "id" or field.endswith("evidence_ids")):
        return mapping.get(value, value)
    return value
