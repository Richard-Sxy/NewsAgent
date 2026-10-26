from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.analytics.entities import ContentType
from app.modules.hot_news.scoring.discovery import DiscoveryRanker, DiscoveryPolicy, validate_policy
from app.modules.hot_news.scoring.metrics import NewsMetricSnapshot


def metric(**changes):
    start = datetime(2026,10,26,tzinfo=timezone.utc)
    row = NewsMetricSnapshot('news',ContentType.ARTICLE,start,start+timedelta(minutes=5),
        impressions=1000,clicks=100,unique_users=80,total_duration_seconds=3000,
        effective_consumptions=80,interactions=20,ctr=D('0.1'))
    return replace(row,**changes)


def ranker():
    return DiscoveryRanker(DiscoveryPolicy().manifest())


def test_small_perfect_rate_cannot_beat_supported_audience():
    r=ranker()
    tiny=metric(news_id='tiny',impressions=1,clicks=1,unique_users=1,effective_consumptions=1,interactions=1)
    big=metric(news_id='big')
    ranked=r.rank([tiny,big],{})
    assert [item.current.news_id for item in ranked]==['big']
    assert r.evidence[1]['qualifies'] is False
    assert r.evaluate(tiny,None)[0].score < r.evaluate(big,None)[0].score


def test_exact_reach_changes_score_even_when_rates_are_identical():
    r=ranker();current=metric()
    low=r.evaluate(replace(current,unique_users=10),None)[0]
    high=r.evaluate(current,None)[0]
    assert high.score>low.score


def test_growth_floor_distinguishes_zero_from_missing_and_limits_small_baseline():
    r=ranker();current=metric()
    zero=metric(clicks=0,effective_consumptions=0,interactions=0)
    one=replace(zero,clicks=1)
    missing,em=r.evaluate(current,None)
    growing,ez=r.evaluate(current,zero)
    nearby,_=r.evaluate(current,one)
    assert missing.growth_component==0 and em['baseline_status']=='missing'
    assert 0<growing.growth_component<D('0.15') and ez['baseline_status']=='zero'
    assert abs(growing.growth_component-nearby.growth_component)<D('0.001')


def test_declining_activity_penalizes_quality_without_negative_score():
    r=ranker();current=metric()
    old=metric(clicks=500,effective_consumptions=400,interactions=100)
    stable,_=r.evaluate(current,current)
    declining,evidence=r.evaluate(current,old)
    assert 0<declining.score<stable.score
    assert declining.growth_component==0 and D(evidence['features']['trend'])<0


def test_repeated_interactions_cannot_create_unbounded_growth():
    r=ranker();current=metric(interactions=80)
    score,_=r.evaluate(current,metric())
    spam,_=r.evaluate(replace(current,interactions=800000),metric())
    assert spam==score


def test_zero_counts_have_zero_score_despite_smoothing_priors():
    score,evidence=ranker().evaluate(metric(impressions=0,clicks=0,unique_users=0,
        effective_consumptions=0,interactions=0),None)
    assert score.score==0 and not evidence['qualifies']


def test_threshold_precedes_top_k_and_ties_are_stable():
    r=ranker();rows=[metric(news_id=n) for n in ('z','a','b')]
    assert [v.current.news_id for v in r.rank(rows,{},limit=2)]==['a','b']
    assert len(r.evidence)==3 and sum(v['selected'] for v in r.evidence)==2
    assert all(v['qualifies'] for v in r.evidence)
    with pytest.raises(ValueError):r.rank([rows[0],rows[0]],{})


def test_frozen_policy_cannot_be_edited_under_the_same_version():
    manifest=DiscoveryPolicy().manifest()
    manifest['entry_score']='0.01'
    with pytest.raises(ValueError):validate_policy(manifest)


@pytest.mark.parametrize('size',[5,15])
def test_components_sum_and_window_specific_support(size):
    current=metric();current=replace(current,window_end=current.window_start+timedelta(minutes=size))
    score,evidence=ranker().evaluate(current,metric())
    assert score.score==sum((score.click_component,score.consumption_component,score.interaction_component,score.growth_component))
    assert 0<=score.score<=1 and evidence['inputs']['window_minutes']==size
    assert evidence['inputs']['unique_users']==80


@pytest.mark.parametrize('changes',[{'unique_users':101},{'impressions':True},{'clicks':-1}])
def test_bad_counter_contract_fails(changes):
    with pytest.raises(ValueError):ranker().evaluate(metric(**changes),None)


def test_foreign_baseline_rejected():
    with pytest.raises(ValueError):ranker().evaluate(metric(),metric(news_id='foreign'))
