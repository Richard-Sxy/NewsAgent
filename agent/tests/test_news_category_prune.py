from copy import deepcopy

import pytest

from app.modules.news_storage.model import build_package, validate_package
from app.modules.news_storage.topic_labels import build_tag_package, validate_tag_package
from tools.prune_public_news_categories import subset_packages
from tests.test_unified_news_tag_store import candidate, legacy
import json
from sqlalchemy import create_engine, text
from simulation.streaming_lab.public_news import retained_content_binding


def packages():
    content=build_package([candidate('keep'),candidate('remove'),candidate('uncertain',title='普通新闻')],tenant_id='test')
    return content,build_tag_package(content,legacy())


def test_subset_rebinds_releases_and_preserves_retained_bytes():
    content,tags=packages()
    original=deepcopy((content,tags))
    result,labels=subset_packages(content,tags,{'keep','uncertain'})
    assert (content,tags)==original
    assert result['manifest']['release_id']!=content['manifest']['release_id']
    assert labels['manifest']['tag_release_id']!=tags['manifest']['tag_release_id']
    assert result['versions']==[r for r in content['versions'] if r['news_id']!='remove']
    assert labels['labels']==[r for r in tags['labels'] if r['news_id']!='remove']
    validate_package(result)
    validate_tag_package(labels,result)


@pytest.mark.parametrize('ids',[set(),{'missing'},{'keep','remove','uncertain'},{'keep','missing'}])
def test_empty_unknown_and_noop_selection_rejected(ids):
    with pytest.raises(ValueError):
        subset_packages(*packages(),ids)


def test_tampered_input_rejected_before_subsetting():
    content,tags=packages()
    content['versions'][0]['body']='changed'
    with pytest.raises(ValueError):
        subset_packages(content,tags,{'keep'})


def test_same_selection_has_stable_release_id():
    a,b=packages()
    assert subset_packages(a,b,{'keep','uncertain'})==subset_packages(a,b,set(['uncertain','keep']))


def test_unpruned_catalog_binding_is_unchanged():
    engine=create_engine('sqlite://')
    manifest={'content_tenant_id':'test','content_release_id':'original','tag_release_id':'tags-old'}
    assert retained_content_binding(engine,manifest)==manifest


@pytest.mark.parametrize('status',['applied_target','applied_verified'])
def test_historical_binding_uses_only_audited_retained_release(status):
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('CREATE TABLE news_content_prune_runs(source_release_id text,target_release_id text,manifest text)'))
        c.execute(text('INSERT INTO news_content_prune_runs VALUES(:old,:new,:manifest)'),
                  {'old':'original','new':'retained','manifest':json.dumps({'tenant_id':'test','status':status,'content_release_id':'retained','tag_release_id':'tags-new'})})
    manifest={'content_tenant_id':'test','content_release_id':'original','tag_release_id':'tags-old','other':'unchanged'}
    result=retained_content_binding(engine,manifest)
    assert result=={**manifest,'content_release_id':'retained','tag_release_id':'tags-new'}
    assert manifest['content_release_id']=='original'
    assert retained_content_binding(engine,{**manifest,'content_tenant_id':'another'})['content_release_id']=='original'


def test_uncommitted_prune_binding_is_rejected():
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('CREATE TABLE news_content_prune_runs(source_release_id text,target_release_id text,manifest text)'))
        c.execute(text('INSERT INTO news_content_prune_runs VALUES(:old,:new,:manifest)'),
                  {'old':'original','new':'retained','manifest':json.dumps({'tenant_id':'test','status':'prepared','content_release_id':'retained','tag_release_id':'tags-new'})})
    with pytest.raises(ValueError,match='incomplete'):
        retained_content_binding(engine,{'content_tenant_id':'test','content_release_id':'original','tag_release_id':'tags-old'})
