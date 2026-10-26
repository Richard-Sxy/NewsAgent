"""Versioned, deterministic discovery policy for the isolated window pipeline.

The constants are candidate business parameters, not statistically calibrated
priors or production thresholds. Published snapshots freeze the entire policy.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from hashlib import sha256
import json

from .hot_score import HotScoreResult
from .ranking import RankedHotNews

D = Decimal
VERSION = 'hot-discovery-v2'


@dataclass(frozen=True)
class DiscoveryPolicy:
    version: str = VERSION
    status: str = 'experimental'
    click_weight: str = '0.30'
    consumption_weight: str = '0.35'
    interaction_weight: str = '0.20'
    growth_weight: str = '0.15'
    ctr_prior: str = '0.05'
    consumption_prior: str = '0.50'
    interaction_prior: str = '0.05'
    ctr_prior_count: int = 100
    consumption_prior_count: int = 20
    interaction_prior_count: int = 20
    minimum_impressions_per_minute: int = 20
    minimum_clicks_per_minute: int = 4
    minimum_uv: int = 10
    uv_scale: int = 50
    activity_floor_per_minute: int = 10
    volume_floor: str = '0.25'
    decline_penalty: str = '0.50'
    entry_score: str = '0.15'

    def manifest(self):
        values = asdict(self)
        raw = json.dumps(values, sort_keys=True, separators=(',', ':'))
        return {**values, 'sha256': sha256(raw.encode()).hexdigest()}


def policy_for(version):
    if version != VERSION:
        raise ValueError('Unknown discovery policy version')
    return DiscoveryPolicy()


def validate_policy(manifest):
    if manifest != policy_for(manifest.get('version')).manifest():
        raise ValueError('Discovery policy differs from its frozen registered version')


class DiscoveryRanker:
    """Score every candidate before thresholding and Top-K; retain rejected reasons."""

    def __init__(self, manifest):
        validate_policy(manifest)
        self.policy = policy_for(manifest['version'])
        self.evidence = []

    def evaluate(self, current, baseline):
        p = self.policy
        width = D(str((current.window_end-current.window_start).total_seconds())) / 60
        if width not in (5, 15):
            raise ValueError('Discovery requires a complete 5/15-minute window')
        counts = (current.impressions, current.clicks, current.unique_users,
                  current.effective_consumptions, current.interactions)
        if any(type(n) is not int or n < 0 for n in counts):
            raise ValueError('Discovery counters must be nonnegative integers')
        if not (current.unique_users <= current.clicks <= current.impressions
                and current.effective_consumptions <= current.clicks):
            raise ValueError('Invalid discovery funnel')
        if baseline is not None:
            if (baseline.news_id, baseline.content_type) != (current.news_id, current.content_type):
                raise ValueError('Discovery baseline identity mismatch')
            if any(not D(n).is_finite() or D(n) < 0 for n in
                   (baseline.clicks, baseline.effective_consumptions, baseline.interactions)):
                raise ValueError('Invalid discovery baseline')
        with localcontext() as ctx:
            ctx.prec = 28
            def smooth(n, denominator, prior, strength):
                return (min(D(n), D(denominator)) + D(prior)*strength)/(D(denominator)+strength)

            ctr = smooth(current.clicks, current.impressions, p.ctr_prior, p.ctr_prior_count)
            consumption = smooth(current.effective_consumptions, current.clicks,
                                 p.consumption_prior, p.consumption_prior_count)
            interaction = smooth(current.interactions, current.effective_consumptions,
                                 p.interaction_prior, p.interaction_prior_count)
            support = min(D(1), D(current.impressions)/(p.minimum_impressions_per_minute*width)) * min(
                D(1), D(current.unique_users)/p.minimum_uv)
            # UV is exact distinct reach over the window, not an additive per-minute count.
            volume = D(current.unique_users)/(current.unique_users+p.uv_scale)
            # Repeated interactions must not create unlimited activity growth.
            activity = D(current.clicks+current.effective_consumptions+
                         min(current.interactions,current.effective_consumptions))
            prior_activity = None if baseline is None else D(
                baseline.clicks+baseline.effective_consumptions+
                min(baseline.interactions,baseline.effective_consumptions))
            trend = D(0)
            if prior_activity is not None:
                delta = activity-prior_activity
                trend = delta/(max(prior_activity, width*p.activity_floor_per_minute)+abs(delta))
            decay = 1+D(p.decline_penalty)*min(D(0), trend)
            quality_factor = support*(D(p.volume_floor)+(1-D(p.volume_floor))*volume)*decay
            parts = (D(p.click_weight)*ctr*quality_factor,
                     D(p.consumption_weight)*consumption*quality_factor,
                     D(p.interaction_weight)*interaction*quality_factor,
                     D(p.growth_weight)*max(D(0), trend)*support)
            score = sum(parts, D(0))
        reasons = []
        if current.impressions < p.minimum_impressions_per_minute*width:
            reasons.append('insufficient_impressions')
        if current.clicks < p.minimum_clicks_per_minute*width:
            reasons.append('insufficient_clicks')
        if current.unique_users < p.minimum_uv:
            reasons.append('insufficient_unique_users')
        if score < D(p.entry_score):
            reasons.append('below_entry_score')
        result = HotScoreResult(current.news_id, score, *parts)
        evidence = dict(news_id=current.news_id, content_type=current.content_type.value,
            qualifies=not reasons, reasons=reasons, selected=False,
            baseline_status='missing' if prior_activity is None else 'zero' if prior_activity == 0 else 'present',
            inputs=dict(impressions=current.impressions, clicks=current.clicks,
                        unique_users=current.unique_users, effective_consumptions=current.effective_consumptions,
                        interactions=current.interactions, window_minutes=int(width)),
            features={k:str(v) for k,v in dict(smoothed_ctr=ctr, smoothed_consumption=consumption,
                smoothed_interaction=interaction, support=support, volume=volume, trend=trend,
                decline_factor=decay, quality_factor=quality_factor).items()},
            components={k:str(v) for k,v in zip(('click','consumption','interaction','growth'),parts)},
            score=str(score))
        return result, evidence

    def rank(self, current_snapshots, baselines, *, limit=20):
        if limit <= 0:
            raise ValueError('limit must be positive')
        self.evidence = []
        seen, scored = set(), []
        for current in current_snapshots:
            key = current.news_id, current.content_type
            if key in seen:
                raise ValueError('Duplicate discovery candidate')
            seen.add(key)
            baseline = baselines.get(key)
            score, detail = self.evaluate(current, baseline)
            self.evidence.append(detail)
            if detail['qualifies']:
                scored.append((current, baseline, score, detail))
        scored.sort(key=lambda item: (-item[2].score, item[0].news_id, item[0].content_type.value))
        ranked = []
        for rank, (current, baseline, score, detail) in enumerate(scored[:limit], 1):
            detail['selected'] = True
            ranked.append(RankedHotNews(rank, current, baseline, score))
        self.evidence.sort(key=lambda item: (item['news_id'],item['content_type']))
        return ranked
