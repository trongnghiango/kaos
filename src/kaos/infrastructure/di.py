"""
Dependency Injection Container for KAOS
=======================================
Đăng ký và kết nối các Ports với Adapters cụ thể (DI Wiring).
Khởi tạo cấu hình và trả về các Use Cases sẵn sàng sử dụng.
"""

from typing import Optional
from pathlib import Path

# Domain models/configs
from kaos.domain.value_objects import ExecutionConfig, SessionMetadata
# Application Ports
from kaos.application.ports import CachePort, GitPort, StoragePort, GatekeeperPort, LLMProviderPort
# Application Use Cases
from kaos.application.use_cases import (
    ExtractSchemaUseCase,
    AnalyzeRequirementsUseCase,
    DetectScopeUseCase,
    ExecuteWorkflowUseCase,
    ClassifyErrorUseCase,
    AnalyzeCompatibilityUseCase,
    ScoutCoordinator,
    ActExecutor,
    GitAutoManager,
)

# Infrastructure Adapters
from kaos.infrastructure.adapters import (
    GitCliAdapter,
    FileStorageAdapter,
    TsGatekeeperAdapter,
    GooseCliAdapter,
    AntigravityAdapter,
    FileCacheAdapter,
)

# Thống nhất constants từ config.py hiện hành
import os
from kaos.config import (
    generate_session_id,
    get_tmp_dir,
    MAX_RETRIES_CODER,
    MAX_RETRIES_PLANNER,
    MAX_RETRIES_ANALYZER,
    TIMEOUT_SECS_CODER,
    TIMEOUT_SECS_PLANNER,
    TIMEOUT_SECS_ANALYZER,
    TIMEOUT_SECS_GATEKEEPER,
    CONFIG,
    TMP_DIR,
    TARGET_PATH,
)


class Container:
    """KAOS Dependency Injection Container"""

    def __init__(
        self,
        target_module: str = "crm",
        branch_name: Optional[str] = None,
        llm_provider: Optional[str] = None,
    ):
        self.target_module = target_module

        # 1. Khởi tạo cấu hình từ config.py
        self.config = ExecutionConfig(
            max_retries_coder=MAX_RETRIES_CODER,
            max_retries_planner=MAX_RETRIES_PLANNER,
            max_retries_analyzer=MAX_RETRIES_ANALYZER,
            timeout_secs_coder=TIMEOUT_SECS_CODER,
            timeout_secs_planner=TIMEOUT_SECS_PLANNER,
            timeout_secs_analyzer=TIMEOUT_SECS_ANALYZER,
            timeout_secs_gatekeeper=TIMEOUT_SECS_GATEKEEPER,
        )

        session_id = generate_session_id()
        actual_branch = branch_name or f"harness/kaos-{target_module}-{session_id.split('_')[0]}"
        self.session_meta = SessionMetadata(
            session_id=session_id,
            target_module=target_module,
            branch_name=actual_branch,
        )
        self.tmp_dir = get_tmp_dir(session_id)

        # 2. Khởi tạo các Adapters (Infrastructure)
        self.git_adapter = GitCliAdapter()
        self.storage_adapter = FileStorageAdapter()
        self.gatekeeper_adapter = TsGatekeeperAdapter()

        # 3. Chọn LLM provider theo priority chain:
        #    CLI arg > ENV var > runner_config.json > default "goose"
        resolved_provider = (
            llm_provider
            or os.environ.get("KAOS_LLM_PROVIDER")
            or CONFIG.get("llm", {}).get("provider", "goose")
        )
        self.llm_adapter = self._create_llm_adapter(resolved_provider)

    def _create_llm_adapter(self, provider_name: str) -> LLMProviderPort:
        """
        Factory method — tạo LLM adapter phù hợp với provider được chỉ định.
        Extend method này khi muốn thêm provider mới.
        """
        provider_cfg = CONFIG.get("llm", {}).get("providers", {}).get(provider_name, {})

        if provider_name == "goose":
            return GooseCliAdapter()

        elif provider_name == "antigravity":
            handshake_dir = TMP_DIR / provider_cfg.get("handshake_dir", "handshake")
            poll_interval = float(provider_cfg.get("poll_interval_secs", 2.0))
            return AntigravityAdapter(
                handshake_dir=handshake_dir,
                poll_interval=poll_interval,
            )

        else:
            raise ValueError(
                f"❌ [KAOS] Unknown LLM provider: '{provider_name}'. "
                f"Supported: 'goose', 'antigravity'. "
                f"Set via --llm-provider, KAOS_LLM_PROVIDER env, or runner_config.json llm.provider"
            )

    # --- Use Case Resolvers ---

    def resolve_extract_schema(self) -> ExtractSchemaUseCase:
        return ExtractSchemaUseCase(gatekeeper=self.gatekeeper_adapter)

    def resolve_analyze_requirements(self) -> AnalyzeRequirementsUseCase:
        return AnalyzeRequirementsUseCase(
            llm_provider=self.llm_adapter,
            storage=self.storage_adapter,
            gatekeeper=self.gatekeeper_adapter,
            config=self.config,
            tmp_dir=self.tmp_dir,
        )

    def resolve_detect_scope(self) -> DetectScopeUseCase:
        return DetectScopeUseCase(
            llm_provider=self.llm_adapter,
            storage=self.storage_adapter,
            gatekeeper=self.gatekeeper_adapter,
            config=self.config,
            tmp_dir=self.tmp_dir,
        )

    def resolve_classify_error(self) -> ClassifyErrorUseCase:
        return ClassifyErrorUseCase(
            llm_provider=self.llm_adapter,
            storage=self.storage_adapter,
            config=self.config,
            tmp_dir=self.tmp_dir,
        )

    def resolve_execute_workflow(self) -> ExecuteWorkflowUseCase:
        return ExecuteWorkflowUseCase(
            git=self.git_adapter,
            storage=self.storage_adapter,
            gatekeeper=self.gatekeeper_adapter,
            llm_provider=self.llm_adapter,
            config=self.config,
            session_meta=self.session_meta,
            tmp_dir=self.tmp_dir,
            classify_error=self.resolve_classify_error(),
        )

    def resolve_analyze_compatibility(self) -> AnalyzeCompatibilityUseCase:
        return AnalyzeCompatibilityUseCase(
            llm_provider=self.llm_adapter,
            storage=self.storage_adapter,
            gatekeeper=self.gatekeeper_adapter,
            config=self.config,
            tmp_dir=self.tmp_dir,
        )

    # ── Scout→Act Resolvers ──────────────────────────────────────

    def resolve_cache(self) -> FileCacheAdapter:
        """Trả về cache adapter (singleton pattern)."""
        if not hasattr(self, "_cache_adapter"):
            self._cache_adapter = FileCacheAdapter()
        return self._cache_adapter

    def resolve_scout_coordinator(self) -> ScoutCoordinator:
        return ScoutCoordinator(
            llm_provider=self.llm_adapter,
            gatekeeper=self.gatekeeper_adapter,
            storage=self.storage_adapter,
            cache=self.resolve_cache(),
            config=self.config,
            tmp_dir=self.tmp_dir,
        )

    def resolve_act_executor(self, target_path: str = "") -> ActExecutor:
        resolved_target = target_path or (str(TARGET_PATH) if TARGET_PATH else str(Path.cwd()))
        return ActExecutor(
            llm_provider=self.llm_adapter,
            gatekeeper=self.gatekeeper_adapter,
            storage=self.storage_adapter,
            cache=self.resolve_cache(),
            config=self.config,
            tmp_dir=self.tmp_dir,
            target_path=resolved_target,
        )

    def resolve_git_auto_manager(self, target_path: str = "") -> GitAutoManager:
        resolved_target = target_path or (str(TARGET_PATH) if TARGET_PATH else str(Path.cwd()))
        return GitAutoManager(
            git=self.git_adapter,
            storage=self.storage_adapter,
            target_path=resolved_target,
        )