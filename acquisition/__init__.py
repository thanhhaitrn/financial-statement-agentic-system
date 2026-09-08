"""Report discovery, secure download, conversion, quota, and import jobs."""

from pathlib import Path

from acquisition.cafef import CafeFReportDiscoveryProvider
from acquisition.download import PdfArtifactValidator, SecurePdfDownloader
from acquisition.llamaparse import LlamaParseDocumentConverter
from acquisition.quota import AcquisitionQuota
from acquisition.service import ReportAcquisitionService
from acquisition.store import AcquisitionStore
from config.runtime_policy import DEFAULT_POLICY, RuntimePolicy


def build_default_acquisition_quota(
    policy: RuntimePolicy = DEFAULT_POLICY,
) -> AcquisitionQuota:
    from dataset_catalog.registry import DATASETS_DIR

    return AcquisitionQuota(
        AcquisitionStore(Path(DATASETS_DIR) / "acquisition" / "acquisition.db"),
        policy.acquisition,
    )


def build_default_acquisition_service(
    policy: RuntimePolicy = DEFAULT_POLICY,
    *,
    llama_client=None,
    allow_cloud_parse: bool = False,
) -> ReportAcquisitionService:
    """Wire the production providers without importing optional SDKs eagerly."""

    from dataset_catalog.registry import DATASETS_DIR, SOURCES_DIR

    root = Path(DATASETS_DIR) / "acquisition"
    validator = PdfArtifactValidator(
        max_file_bytes=policy.acquisition.max_file_bytes,
        max_pdf_pages=policy.acquisition.max_pdf_pages,
    )
    return ReportAcquisitionService(
        store=AcquisitionStore(root / "acquisition.db"),
        discovery_provider=CafeFReportDiscoveryProvider(
            timeout_seconds=policy.acquisition.download_timeout_seconds,
        ),
        converter=LlamaParseDocumentConverter(
            allow_cloud_parse=allow_cloud_parse,
            client=llama_client,
            cache_dir=root / "llamaparse-cache",
            tier=policy.acquisition.llamaparse_tier,
            version=policy.acquisition.llamaparse_version,
            max_retries=policy.acquisition.provider_max_retries,
            circuit_failure_threshold=(
                policy.acquisition.provider_circuit_failure_threshold
            ),
            circuit_cooldown_seconds=(
                policy.acquisition.provider_circuit_cooldown_seconds
            ),
        ),
        validator=validator,
        downloader=SecurePdfDownloader(
            validator=validator,
            max_file_bytes=policy.acquisition.max_file_bytes,
            timeout_seconds=policy.acquisition.download_timeout_seconds,
        ),
        policy=policy.acquisition,
        artifact_dir=root / "artifacts",
        # Managed sources must stay below the registry's ownership boundary.
        converted_dir=Path(SOURCES_DIR) / "imports",
        symbol_catalog_path=Path(DATASETS_DIR) / "web" / "cafef_symbols.json",
    )

__all__ = [
    "CafeFReportDiscoveryProvider",
    "LlamaParseDocumentConverter",
    "ReportAcquisitionService",
    "build_default_acquisition_quota",
    "build_default_acquisition_service",
]
