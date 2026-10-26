import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import news_annotation_policy as policy
import publish_news_annotations as publisher


@pytest.mark.parametrize('title,text,original,expected',[
    ('AI芯片公司获维持买入评级','公司发布模型','ai','stocks'),
    ('cvMax谈上单卡莎','虎扑体育内容，LCK季后赛','sports','games'),
    ('小鹿鼓励AG','虎扑官方账号','entertainment','games'),
    ('汉密尔顿在新加坡取胜','排位赛杆位','society','sports'),
    ('佛罗伦萨前锋贝托谈比赛','','entertainment','sports'),
    ('全国首款仿生医疗机器人发布','具备情感大模型','health','tech'),
    ('AI医疗专家共识发布','医疗场景','health','ai'),
    ('日本北海道农场虐待劳工','海外社会事件','society','international'),
    ('日本前锋谈新赛季','','society','sports'),
    ('出生顺序影响老大与老二性格','家庭育儿','education','lifestyle'),
    ('腾讯自由现金流转负','资本开支','ai','finance'),
    ('重卡撞击护栏','交通事故','society','society'),
    ('孩子沉迷王者荣耀如何解决','家庭问题','lifestyle','lifestyle'),
    ('社区老人的日常生活','没有体育内容','society','society'),
    ('微信单删提示引争议','微信功能','insufficient','tech'),
    ('富士康招工冲刺iPhone量产','招聘薪酬','finance','finance'),
    ('F1车手谈赛车表现','','society','sports'),
    ('9月3日版本更新公告','高能英雄','insufficient','games'),
    ('无根的精神处境','译者谈哲学著作和思想者的书写','insufficient','culture'),
])
def test_editorial_boundaries_preserve_actual_story_scope(title,text,original,expected):
    row={'title':title,'excerpts':[text],'primary_category':original}
    assert policy.decide(row)[0]==expected


@pytest.fixture
def annotation_input(tmp_path,monkeypatch):
    rows=[{'news_id':str(i),'record_sha256':'a'*64,'title':'医疗服务进社区',
           'title_source':'record_title','content_state':'title_only','excerpts':[],
           'previous_action':'hold_unclassified','previous_primary':'unclassified','source_flags':[]}
          for i in range(4714)]
    manifest={'version':publisher.VERSION,'source_release_id':'release-test','tenant_id':'test','prompt_sha256':'b'*64}
    predictions=[{**r,'primary_category':'health','category_scores':{'health':.9,'society':.1},
                  'score_margin':.8,'prompt_sha256':'b'*64,'version':publisher.VERSION} for r in rows]
    monkeypatch.setattr(publisher,'inputs',lambda:({'tenant_id':'test','content_release_id':'release-test'},rows))
    (tmp_path/'inference-manifest.json').write_text(json.dumps(manifest))

    def write(pred=predictions):
        (tmp_path/'model-predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in pred))
    write()
    return tmp_path,predictions,write


def test_complete_labels_keep_outside_topics_and_cap_title_confidence(annotation_input):
    directory,_,_=annotation_input
    result=publisher.finalize(directory)
    assert result['scope_counts']=={'outside_target':4714}
    assert result['confidence_counts']=={'medium':4714}
    assert result['manual_annotation_required']==0 and result['deleted_news']==0
    assert publisher.finalize(directory)==result


def test_duplicate_or_partial_annotation_set_rejected(annotation_input):
    directory,predictions,write=annotation_input
    write(predictions[:-1]+[predictions[0]])
    with pytest.raises(ValueError,match='unique'):
        publisher.finalize(directory)


def test_annotation_cannot_change_source_text(annotation_input):
    directory,predictions,write=annotation_input
    predictions[0]['title']='tampered'
    write()
    with pytest.raises(ValueError,match='source identity'):
        publisher.finalize(directory)


def test_mixed_model_policy_rejected(annotation_input):
    directory,predictions,write=annotation_input
    predictions[0]['prompt_sha256']='different'
    write()
    with pytest.raises(ValueError,match='Mixed'):
        publisher.finalize(directory)


def test_unknown_category_rejected(annotation_input):
    directory,predictions,write=annotation_input
    predictions[0]['primary_category']='made-up'
    write()
    with pytest.raises(ValueError,match='Invalid category'):
        publisher.finalize(directory)


def test_assistant_adjudication_requires_matching_source_evidence(annotation_input):
    directory,_,_=annotation_input
    override={'news_id':'0','record_sha256':'a'*64,'category':'society','confidence':'low',
              'reason':'社区服务主题','evidence_quote':'医疗服务进社区','human_verified':False}
    (directory/'assistant-adjudications.json').write_text(json.dumps([override]))
    result=publisher.finalize(directory)
    assert result['assistant_adjudication_count']==1
    row=json.loads((directory/'annotations.jsonl').read_text().splitlines()[0])
    assert row['primary_category']=='society' and row['confidence']=='low'
    assert row['evidence']['assistant_adjudication']==override


def test_fabricated_adjudication_evidence_rejected(annotation_input):
    directory,_,_=annotation_input
    override={'news_id':'0','record_sha256':'a'*64,'category':'society','confidence':'low',
              'reason':'测试','evidence_quote':'不在原文中的证据'}
    (directory/'assistant-adjudications.json').write_text(json.dumps([override]))
    with pytest.raises(ValueError,match='adjudication identity/evidence'):
        publisher.finalize(directory)
