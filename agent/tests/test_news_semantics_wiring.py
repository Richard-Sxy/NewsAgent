"""The opt-in YAML must wire all runtime paths; deterministic substitutes cannot masquerade as inference."""
import pytest
import yaml

from app.modules.knowledge.news_semantics_store import metadata
from app.modules.knowledge.news_semantics_contracts import SemanticConfig
from tests.test_tier_lifecycle_wiring import setup
from tests.test_news_semantics import ScriptedInference


def config_path(tmp_path, model):
    cfg = SemanticConfig(model_route=model.inference.model_routes[0], model_version="fixture-v1")
    path = tmp_path / "semantic.yml"
    path.write_text(yaml.safe_dump(cfg.model_dump()), encoding="utf-8")
    return str(path)


def test_semantics_rejects_missing_port_and_fake_local_model(setup, tmp_path):
    path = config_path(tmp_path, setup.model)
    with pytest.raises(ValueError, match="inference Port"):
        setup.build(semantic_config_path=path)
    with pytest.raises(ValueError, match="deterministic local"):
        setup.build(semantic_config_path=path, inference=ScriptedInference())


@pytest.mark.asyncio
async def test_semantics_factory_uses_shared_service_and_checks_migration(setup, tmp_path):
    model = setup.model.model_copy(update={"inference": setup.model.inference.model_copy(
        update={"provider": "openai_compatible"})})
    runtime = setup.build(model_config=model, semantic_config_path=config_path(tmp_path, model),
                          inference=ScriptedInference())
    assert runtime.service.semantics is runtime.query.semantics
    with pytest.raises(Exception):
        await runtime.validate()
    metadata.create_all(setup.database.engine)
    await runtime.validate()
