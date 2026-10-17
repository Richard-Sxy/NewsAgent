"""Candidate presets come exclusively from the validated deployment manifest."""

import json

from app.schemas.production_bundle import ProductionBundleSpec


def candidate_templates(manifest_json: str, runtime_registry) -> dict:
    specs = [ProductionBundleSpec.model_validate(item) for item in json.loads(manifest_json)]
    for spec in specs:
        runtime_registry.ensure_supported(spec)
    unique = {spec.model_dump_json(): spec for spec in specs}
    if len(unique) < 2:
        return {}
    templates, ambiguous = {}, set()
    for spec in unique.values():
        for parameter in ("model_version", "analysis_prompt_version"):
            key = parameter, getattr(spec, parameter)
            if key in templates and templates[key] != spec:
                ambiguous.add(key)
            templates[key] = spec
    return {key: spec for key, spec in templates.items() if key not in ambiguous}
