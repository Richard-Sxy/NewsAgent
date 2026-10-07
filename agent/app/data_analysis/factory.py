"""Select the bounded analytics backend independently for each caller."""

from .remote import RemoteAnalysisRunner
from .runner import AnalysisRunner


def create_analysis_runner(settings, *, enabled=None):
    if not (settings.conversation_data_analysis_enabled if enabled is None else enabled):
        return None
    options = {"timeout_seconds": settings.data_analysis_timeout_seconds,
               "max_rows": settings.data_analysis_max_rows,
               "max_input_bytes": settings.data_analysis_max_input_bytes,
               "max_output_bytes": settings.data_analysis_max_output_bytes,
               "max_concurrency": settings.data_analysis_max_concurrency,
               "memory_mb": settings.data_analysis_memory_mb}
    if settings.data_analysis_backend == "service":
        return RemoteAnalysisRunner(
            url=settings.data_analysis_service_url,
            token=settings.data_analysis_service_token.get_secret_value(),
            allow_insecure_http=settings.data_analysis_service_allow_insecure_http,
            require_os_limits=settings.data_analysis_service_require_os_limits,
            **options,
        )
    return AnalysisRunner(backend=settings.data_analysis_backend,
                          docker_image=settings.data_analysis_docker_image, **options)
