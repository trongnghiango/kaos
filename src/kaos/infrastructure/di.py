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
from kaos.application.ports import CachePort, GitPort, StoragePort, GatekeeperPort, LLMProviderPort, KnowledgeGraphPort, NotificationPort
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
    ClaudeCodeAdapter,
    AntigravityAdapter,
    ClaudeCodeAdapter,
    FileCacheAdapter,
    RedisGraphAdapter,
    TelegramAdapter,
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
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_MONITOR_ENABLED,
    logger,
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
        self.knowledge_graph = RedisGraphAdapter()

        # ── Telegram Monitor ──────────────────────────────────────
        if TELEGRAM_MONITOR_ENABLED and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            from kaos.application.ports import NotificationPort
            self.telegram = TelegramAdapter(
                token=TELEGRAM_TOKEN,
                chat_id=TELEGRAM_CHAT_ID,
            )
            asyncio.ensure_future(self._register_telegram_commands())
            logger.info("📲 Telegram monitor ENABLED")
        else:
            self.telegram = None
            logger.debug("📵 Telegram monitor DISABLED (set TELEGRAM_MONITOR_ENABLED=true)")

        # 3. Chọn LLM provider theo priority chain:
        #    CLI arg > ENV var > runner_config.json > default "goose"
        resolved_provider = (
            llm_provider
            or os.environ.get("KAOS_LLM_PROVIDER")
            or CONFIG.get("llm", {}).get("provider", "goose")
        )
        self.llm_adapter = self._create_llm_adapter(resolved_provider)
        # Register Telegram commands if bot is enabled
        if self.telegram:
            self._register_telegram_commands()

    def _create_llm_adapter(self, provider_name: str) -> LLMProviderPort:
        """
        Factory method — tạo LLM adapter phù hợp với provider được chỉ định.
        Extend method này khi muốn thêm provider mới.
        """
        provider_cfg = CONFIG.get("llm", {}).get("providers", {}).get(provider_name, {})

        if provider_name == "goose":
            return GooseCliAdapter()

        elif provider_name == "claude-code":
            return ClaudeCodeAdapter()

        elif provider_name == "antigravity":
            handshake_dir = TMP_DIR / provider_cfg.get("handshake_dir", "handshake")
            poll_interval = float(provider_cfg.get("poll_interval_secs", 2.0))
            return AntigravityAdapter(
                handshake_dir=handshake_dir,
                poll_interval=poll_interval,
            )

        elif provider_name == "claude-code":
            return ClaudeCodeAdapter()

        else:
            raise ValueError(
                f"❌ [KAOS] Unknown LLM provider: '{provider_name}'. "
                f"Supported: 'goose', 'claude-code', 'antigravity'. "
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
            knowledge_graph=self.knowledge_graph,
            notification=self.telegram,
        )

    def resolve_git_auto_manager(self, target_path: str = "") -> GitAutoManager:
        resolved_target = target_path or (str(TARGET_PATH) if TARGET_PATH else str(Path.cwd()))
        return GitAutoManager(
            git=self.git_adapter,
            storage=self.storage_adapter,
            target_path=resolved_target,
        )

    # ── Telegram Monitor & Control Helpers ───────────────────────

    def _register_telegram_commands(self) -> None:
        """Đăng ký command handler cho bot Telegram để giám sát và điều khiển"""
        if not self.telegram:
            return

        async def cmd_status(chat_id: str, args: str):
            # Truy vấn nhanh trạng thái qua engine/container
            # Tìm các task đang chạy trong background (nếu có lưu vết)
            status_text = (
                f"ℹ️ *KAOS Pipeline Status*\n"
                f"• Session ID: `{self.session_meta.session_id}`\n"
                f"• Module: `{self.target_module}`\n"
                f"• Target branch: `{self.session_meta.branch_name}`\n"
            )
            # Thống kê từ Graph
            try:
                stats = await self.knowledge_graph.get_graph_stats()
                status_text += (
                    f"• Graph Nodes: Tasks={stats['tasks']}, Conditions={stats['conditions']}, Results={stats['results']}\n"
                )
            except Exception as e:
                status_text += f"• Graph Error: {e}\n"

            await self.telegram.send_message(status_text)

        async def cmd_kill(chat_id: str, args: str):
            target_task = args.strip()
            if not target_task:
                await self.telegram.send_message("❌ Vui lòng cung cấp task_id (Ví dụ: `/kill T1`)")
                return

            # Lưu ý: Sẽ liên kết trực tiếp tới active task list trong TaskQueueEngine qua Global/Active engine references
            # Hiện tại gửi tín hiệu dừng thông qua Redis (như một Duyên - feedback: user_terminated)
            try:
                await self.knowledge_graph.upsert_condition(
                    cond_id=f"cond_kill_{target_task}_{int(time.time())}",
                    type="feedback",
                    content=f"FORCE_TERMINATED: Lệnh dừng từ Telegram.",
                )
                # Đánh dấu Task thất bại ngay lập tức để vòng lặp tiếp theo dừng
                await self.telegram.send_message(f"🛑 Đã gửi lệnh tắt nóng task `{target_task}` lên Redis.")
            except Exception as e:
                await self.telegram.send_message(f"❌ Lỗi gửi lệnh: {e}")

        async def cmd_killall(chat_id: str, args: str):
            # Gửi tín hiệu dừng toàn bộ pipeline
            try:
                await self.knowledge_graph.upsert_condition(
                    cond_id=f"cond_killall_{int(time.time())}",
                    type="feedback",
                    content="FORCE_TERMINATED_ALL: Lệnh dừng toàn bộ hệ thống.",
                )
                await self.telegram.send_message("🛑 Đã phát lệnh DỪNG TOÀN BỘ tiến trình đang chạy.")
            except Exception as e:
                await self.telegram.send_message(f"❌ Lỗi: {e}")

        self.telegram.register_command("status", cmd_status)
        self.telegram.register_command("kill", cmd_kill)
        self.telegram.register_command("killall", cmd_killall)
        logger.info("   🤖 Telegram commands registered: /status, /kill, /killall")
