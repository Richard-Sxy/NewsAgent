"""ASR 通道装配的离线测试：三种鉴权形态与 fail-closed 行为。

腾讯内部接入通常不签发云 API 密钥，而是走内网网关（注入头）或免签，
因此装配层必须支持这两种形态，且缺项时直接失败而不是"悄悄用公网凭据"。
"""

import pytest

from app.clients.enterprise.tencent_asr import (
    NoAuthSigner,
    StaticHeaderSigner,
    Tc3RequestSigner,
)
from app.knowledge.bootstrap import VideoIngestSettings, build_asr_rpc


def make_settings(**overrides) -> VideoIngestSettings:
    params: dict[str, object] = {"video_asr_enabled": True}
    params.update(overrides)
    return VideoIngestSettings(**params)


def test_tc3_mode_requires_credentials() -> None:
    with pytest.raises(ValueError, match="VIDEO_ASR_SECRET_ID"):
        build_asr_rpc(make_settings(video_asr_auth_mode="tc3"))

    with pytest.raises(ValueError, match="VIDEO_ASR_SECRET_KEY"):
        build_asr_rpc(
            make_settings(video_asr_auth_mode="tc3", video_asr_secret_id="id")
        )


def test_tc3_mode_defaults_to_public_endpoint() -> None:
    rpc = build_asr_rpc(
        make_settings(
            video_asr_auth_mode="tc3",
            video_asr_secret_id="id",
            video_asr_secret_key="key",
        )
    )
    assert isinstance(rpc._signer, Tc3RequestSigner)
    assert rpc._endpoint == "https://asr.tencentcloudapi.com"


def test_tc3_mode_accepts_internal_endpoint() -> None:
    """内网域名也要能用，且签名会跟着 endpoint 的 host 走。"""

    rpc = build_asr_rpc(
        make_settings(
            video_asr_auth_mode="tc3",
            video_asr_secret_id="id",
            video_asr_secret_key="key",
            video_asr_endpoint="https://asr.internal.tencentcloudapi.com",
        )
    )
    assert rpc._endpoint == "https://asr.internal.tencentcloudapi.com"
    assert isinstance(rpc._signer, Tc3RequestSigner)


def test_gateway_mode_requires_headers_and_endpoint() -> None:
    with pytest.raises(ValueError, match="VIDEO_ASR_GATEWAY_HEADERS"):
        build_asr_rpc(
            make_settings(
                video_asr_auth_mode="gateway",
                video_asr_endpoint="http://gw.internal",
            )
        )
    with pytest.raises(ValueError, match="VIDEO_ASR_ENDPOINT"):
        build_asr_rpc(
            make_settings(
                video_asr_auth_mode="gateway",
                video_asr_gateway_headers={"X-Ticket": "t"},
            )
        )


def test_gateway_mode_builds_static_header_signer() -> None:
    rpc = build_asr_rpc(
        make_settings(
            video_asr_auth_mode="gateway",
            video_asr_endpoint="http://gw.internal",
            video_asr_gateway_headers={"X-Ticket": "ticket-1"},
        )
    )
    assert isinstance(rpc._signer, StaticHeaderSigner)
    assert rpc._signer.headers == {"X-Ticket": "ticket-1"}


def test_none_mode_requires_endpoint_and_builds_no_auth_signer() -> None:
    with pytest.raises(ValueError, match="VIDEO_ASR_ENDPOINT"):
        build_asr_rpc(make_settings(video_asr_auth_mode="none"))

    rpc = build_asr_rpc(
        make_settings(
            video_asr_auth_mode="none", video_asr_endpoint="http://gw.internal"
        )
    )
    assert isinstance(rpc._signer, NoAuthSigner)


def test_unknown_auth_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported VIDEO_ASR_AUTH_MODE"):
        build_asr_rpc(make_settings(video_asr_auth_mode="oauth"))


def test_callback_url_and_polling_settings_are_passed_through() -> None:
    rpc = build_asr_rpc(
        make_settings(
            video_asr_auth_mode="tc3",
            video_asr_secret_id="id",
            video_asr_secret_key="key",
            video_asr_callback_url="https://internal.example.com/asr/callback",
            video_asr_poll_interval_seconds=1.5,
            video_asr_max_poll_attempts=7,
            video_asr_strict_media_format=False,
        )
    )
    assert rpc._callback_url == "https://internal.example.com/asr/callback"
    assert rpc._poll_interval == 1.5
    assert rpc._max_polls == 7
    assert rpc._strict_media_format is False
