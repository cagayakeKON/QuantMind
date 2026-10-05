"""Market declarations shared by model consumers."""

import json


def _mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def declared_model_market(metadata):
    """Use the registry's explicit market, then its context declaration."""
    metadata = _mapping(metadata)
    context = _mapping(metadata.get("context"))
    return str(metadata.get("market") or context.get("market") or "").strip().upper()


def model_context_market(metadata):
    return (
        str(_mapping(_mapping(metadata).get("context")).get("market") or "")
        .strip()
        .upper()
    )


def inference_model_market(metadata, default="CN"):
    """Recognize both JP formats while retaining legacy context-only routing."""
    metadata = _mapping(metadata)
    context = _mapping(metadata.get("context"))
    declared = declared_model_market(metadata)
    if declared == "JP" or str(context.get("market") or "").strip().upper() == "JP":
        return declared
    return str((metadata.get("context") or {}).get("market") or default).upper()


def model_context(metadata):
    """Read optional context fields in either registered metadata format."""
    return _mapping(_mapping(metadata).get("context"))
