"""Read-only Port for a frozen, reconciled realtime audience window."""
from app.modules.audience.taxonomy import TopicCatalog
from app.modules.reader_profiles.contracts import (ReaderProfileBatch, ReaderProfileQuery,
    ReaderProfileProviderError, PROFILE_POLICY_VERSION, reader_profile_query_fingerprint)
from app.modules.reader_profiles.service import ReaderProfileService
from simulation.streaming_lab.realtime_readers import utc
from simulation.streaming_lab.public_news import digest


class RealtimeReaderPort:
    expected_source_mode='synthetic'
    expected_profile_policy_version=PROFILE_POLICY_VERSION
    environment='e2e'

    def __init__(self, session, row):
        self.row=row
        self.source_release_sha256=session['manifest']['identity_sha256']
        self.expected_taxonomy_version=session['manifest']['catalog']['taxonomy_version']
        self.identity_sha256=digest(dict(policy='realtime-frozen-reader-port-v1',snapshot=row['identity_sha256']))

    async def fetch(self, query: ReaderProfileQuery):
        r=self.row
        if (str(query.tenant_id)!=r['tenant_id'] or query.window_start!=utc(r['start_ms'])
                or query.window_end!=utc(r.get('end_ms',r['start_ms']+60000)) or query.source_release_sha256!=self.source_release_sha256):
            raise ReaderProfileProviderError('invalid_query')
        items=[i for i in r['items'] if i['news_id'] in query.news_ids]
        if len(items)!=len(query.news_ids):
            raise ReaderProfileProviderError('invalid_query')
        return ReaderProfileBatch(tenant_id=query.tenant_id,window_start=query.window_start,window_end=query.window_end,
            source_release_sha256=self.source_release_sha256,query_fingerprint=reader_profile_query_fingerprint(query),
            snapshot_id=r['identity_sha256'],source_revision=r['revision'],taxonomy_version=self.expected_taxonomy_version,
            profile_policy_version=self.expected_profile_policy_version,generated_at=r['generated_at'],
            watermark=utc(r['watermark_ms']),is_complete=True,source_mode='synthetic',items=items)

    async def close(self):
        pass


def service(session, row):
    return ReaderProfileService(RealtimeReaderPort(session,row),
        catalog=TopicCatalog.model_validate(session['manifest']['catalog']),environment='e2e')
