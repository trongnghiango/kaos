import time
import json
import re
from pathlib import Path
from typing import Optional, Any, Tuple
from kaos.config import TARGET_PATH, TMP_DIR, logger
from kaos.domain.value_objects import ExecutionConfig
from kaos.domain.scout_results import ScoutReport

class ExecutePipelineUseCase:
    """
    Coordinating use case that orchestrates the execution of either the legacy manual queue pipeline
    or the automated Scout->Act pipeline.
    This separates orchestration business logic from the CLI presentation layer.
    """
    def __init__(self, container_factory):
        self.container_factory = container_factory

    async def execute_manual(self, args) -> int:
        start_time = time.time()

        # Resolve spec
        spec_input = args.spec if args.spec else None
        if spec_input:
            spec_path = Path(spec_input)
            if not spec_path.is_absolute():
                cwd_spec_path = (Path.cwd() / spec_path).resolve()
                target_spec_path = (TARGET_PATH / spec_path).resolve()
                if cwd_spec_path.exists():
                    spec_input = str(cwd_spec_path)
                elif target_spec_path.exists():
                    spec_input = str(target_spec_path)
                elif spec_input.endswith(('.md', '.txt', '.json')) or '/' in spec_input or '\\' in spec_input:
                    logger.error(f"❌ File spec không tồn tại tại: {spec_input} (CWD: {Path.cwd()}, Target: {TARGET_PATH})")
                    return 1
            elif not spec_path.exists():
                logger.error(f"❌ File spec không tồn tại tại: {spec_input}")
                return 1

        # Resolve raw_data
        raw_data_path = args.raw_data if args.raw_data else None
        if raw_data_path:
            raw_path = Path(raw_data_path)
            if not raw_path.is_absolute():
                cwd_raw_path = (Path.cwd() / raw_path).resolve()
                target_raw_path = (TARGET_PATH / raw_path).resolve()
                if cwd_raw_path.exists():
                    raw_data_path = str(cwd_raw_path)
                elif target_raw_path.exists():
                    raw_data_path = str(target_raw_path)

        # Fallback values if status/rerun flags are active without inputs
        if args.status or args.rerun_failed:
            if not raw_data_path and not spec_input:
                raw_data_path = "dummy"

        if not raw_data_path and not spec_input:
            logger.error("❌ Thiếu đầu vào: cần ít nhất raw_data (file Excel/CSV) hoặc --spec (file/chuỗi yêu cầu).")
            return 1

        # Auto dry-run/compatibility analysis trigger
        is_execution_mode = args.resume or args.rerun_failed or args.phase == "execute"
        if not is_execution_mode and not args.compatibility_report:
            args.compatibility_report = str(Path.cwd() / "db_compatibility_report.md")
            args.run_dry = True
            logger.info(f"💡 [KAOS Orchestrator] Tự động kích hoạt chế độ Phân tích tương thích & Ra quyết định tối ưu (Dry-Run)")
            logger.info(f"   Báo cáo quyết định sẽ xuất ra: {args.compatibility_report}")

        # 1. Auto-detect module
        detected_scope = None
        if args.module == "auto":
            temp_container = self.container_factory("system", args.branch, getattr(args, "llm_provider", None))
            detect_use_case = temp_container.resolve_detect_scope()
            try:
                detected_scope = await detect_use_case.execute(spec=spec_input, raw_data=raw_data_path)
                args.module = detected_scope.get("recommended_module", "all")
                logger.info(f"🎯 [KAOS Scope Detector] Đã tự động nhận diện module đích: '{args.module}' (Confidence: {detected_scope.get('confidence_score')})")
            except Exception as e:
                logger.error(f"❌ Tự động nhận diện module thất bại: {e}. Sử dụng fallback module='all'.")
                args.module = "all"
        else:
            logger.info(f"⚙️ [KAOS] Sử dụng module chỉ định cứng: '{args.module}'")

        output_csv = TMP_DIR / f"goose_out_data_analyzer_{args.module}.csv"

        # 2. Resolve main Container
        container = self.container_factory(args.module, args.branch, getattr(args, "llm_provider", None))

        # 3. View status
        if args.status:
            # We will show status using helper or read queue status
            # For simplicity, resolve storage and show status
            tasks_dict = container.storage_adapter.load_queue_tasks(output_csv, args.module, resume=False)
            logger.info(f"\n📊 [KAOS] Trạng thái Task Queue ({args.module}):")
            for t in tasks_dict.values():
                logger.info(f"   - [{t.status}] {t.task_id}: {t.title}")
            return 0

        # 4. DB Compatibility Report (Dry Run)
        if args.compatibility_report or args.run_dry:
            report_path = args.compatibility_report or "tools/kaos/tmp/db_compatibility_report.md"
            report_path_obj = Path(report_path)
            if not report_path_obj.is_absolute():
                if args.compatibility_report:
                    report_path = str((Path.cwd() / report_path_obj).resolve())
                else:
                    report_path = str((TARGET_PATH / "tools" / "kaos" / "tmp" / "db_compatibility_report.md").resolve())
            else:
                report_path = str(report_path_obj.resolve())
            if not raw_data_path:
                logger.error("❌ Phân tích độ tương thích database yêu cầu đầu vào raw_data (đường dẫn file Excel .xlsx).")
                return 1
            
            resolved_raw_path = Path(raw_data_path).resolve()
            if not resolved_raw_path.exists():
                logger.error(f"❌ File raw_data không tồn tại tại: {raw_data_path}")
                return 1

            comp_use_case = container.resolve_analyze_compatibility()
            try:
                await comp_use_case.execute(
                    raw_data=str(resolved_raw_path),
                    spec=spec_input,
                    report_path=report_path,
                    run_dry=args.run_dry
                )
                return 0
            except Exception as e:
                logger.error(f"❌ Phân tích độ tương thích thất bại: {e}")
                return 1

        if args.phase == "execute" and not output_csv.exists():
            logger.info("⚠️ Không thấy file CSV đã phân tích trước đó, tự động chuyển về phase 'all'.")
            args.phase = "all"

        # 5. Extract Schema Phase
        if args.phase in ("all", "extract"):
            extract_use_case = container.resolve_extract_schema()
            try:
                schema = await extract_use_case.execute()
                logger.info(f"✅ [Extract Schema] Hoàn tất. Trích xuất thành công {len(schema)} file schema.")
                if args.phase == "extract":
                    return 0
            except Exception as e:
                logger.error(f"❌ Trích xuất Schema thất bại: {e}")
                return 1

        # 6. Analyze Requirements Phase
        if args.phase in ("all", "analyze"):
            analyze_use_case = container.resolve_analyze_requirements()
            try:
                output_csv = await analyze_use_case.execute(
                    target_module=args.module,
                    output_csv=output_csv,
                    raw_data=raw_data_path,
                    spec=spec_input
                )
                logger.info(f"✅ [Analyze Requirements] Đã tạo Task Queue CSV tại: {output_csv}")
                if args.phase == "analyze":
                    return 0
            except Exception as e:
                logger.error(f"❌ Phân tích yêu cầu thất bại: {e}")
                return 1

        # 7. Execute Tasks Phase
        if args.phase in ("all", "execute"):
            execute_use_case = container.resolve_execute_workflow()
            try:
                success = await execute_use_case.execute(
                    csv_path=output_csv,
                    resume=args.resume,
                    rerun_failed=args.rerun_failed
                )
                logger.info(f"\n⏱️ Tổng thời gian chạy pipeline: {time.time() - start_time:.2f}s")
                return 0 if success else 1
            except Exception as e:
                logger.error(f"❌ Thực thi workflow thất bại: {e}")
                return 1

        return 0

    async def execute_auto(self, args) -> int:
        start_time = time.time()
        logger.info("🤖 [KAOS Auto] Scout→Act Pipeline started")

        # 1. Resolve --status
        if getattr(args, "status", False):
            status_path = TMP_DIR / "engine_status.json"
            if status_path.exists():
                try:
                    data = json.loads(status_path.read_text())
                    logger.info(f"\n📊 [KAOS Auto] Engine Status:")
                    logger.info(f"   Branch : {data.get('branch_name', 'N/A')}")
                    logger.info(f"   Tasks  : {data.get('total', 0)} total, "
                                f"{data.get('completed', 0)} completed, "
                                f"{data.get('failed', 0)} failed")
                    for t in data.get("tasks", []):
                        logger.info(f"   - [{t.get('status','?')}] {t.get('task_id','')} : {t.get('title','')[:60]}")
                except Exception as e:
                    logger.error(f"❌ Cannot read engine status: {e}")
            else:
                logger.warning("⚠️ No engine status file found.")
            return 0

        target_path = str(TARGET_PATH) if TARGET_PATH else str(Path.cwd())

        # Resolve spec
        spec_input = args.spec if args.spec else None
        if spec_input:
            spec_path = Path(spec_input)
            if not spec_path.is_absolute():
                cwd_path = (Path.cwd() / spec_path).resolve()
                if cwd_path.exists():
                    spec_input = str(cwd_path)
            elif spec_path.exists():
                spec_input = spec_path.read_text(encoding="utf-8")

        # Resolve raw_data
        raw_data = args.raw_data if args.raw_data else None
        if raw_data:
            raw_path = Path(raw_data)
            if not raw_path.is_absolute():
                cwd_raw = (Path.cwd() / raw_path).resolve()
                if cwd_raw.exists():
                    raw_data = str(cwd_raw)

        # 2. Auto-detect module
        module = args.module
        if module == "auto":
            temp_container = self.container_factory("system", args.branch, getattr(args, "llm_provider", None))
            detect_use_case = temp_container.resolve_detect_scope()
            try:
                detected_scope = await detect_use_case.execute(spec=spec_input, raw_data=raw_data)
                module = detected_scope.get("recommended_module", "all")
                logger.info(f"🎯 [KAOS Auto] Detected module: '{module}'")
            except Exception as e:
                logger.warning(f"   ⚠️ Auto-detect failed: {e}. Using fallback='all'.")
                module = "all"

        # 3. Initialize container
        container = self.container_factory(module, args.branch, getattr(args, "llm_provider", None))

        # 4. Git Auto Branch (Mode B)
        phase = getattr(args, "phase", "all")
        git_branch = ""
        git_mgr = None
        if phase != "act" and getattr(args, "git_auto", True):
            logger.info("🔀 [KAOS Auto] Setting up git branch...")
            git_mgr = container.resolve_git_auto_manager(target_path=target_path)
            git_ok, git_branch = await git_mgr.setup_branch(
                module=module,
                description=spec_input[:40] if spec_input else "",
            )
            if git_ok:
                logger.info(f"   ✅ Working on branch: {git_branch}")
            else:
                logger.warning("   ⚠️  Git branch setup failed — continuing without isolation")

        # 5. Scout Phase
        report = None
        if phase != "act":
            logger.info("🔍 [KAOS Auto] Scout Phase — analyzing codebase + spec...")
            scout = container.resolve_scout_coordinator()
            report = await scout.execute(
                raw_data=raw_data,
                spec=spec_input,
                target_path=target_path,
                force_reparse=getattr(args, "force_reparse", False),
            )
            logger.info(
                f"   ✅ Scout complete: module={report.module}, "
                f"compatibility={report.compatibility_score}%, "
                f"conflicts={len(report.conflict_points)}, "
                f"confidence={report.confidence_level}"
            )

            if phase == "scout":
                elapsed = time.time() - start_time
                logger.info(f"\n🏁 [KAOS Auto] Scout-only phase complete in {elapsed:.1f}s")
                return 0
        else:
            cached_report_path = TMP_DIR / "scout_report.json"
            if cached_report_path.exists():
                try:
                    data = json.loads(cached_report_path.read_text())
                    report = ScoutReport(**data)
                    logger.info(f"   ✅ Loaded cached ScoutReport from {cached_report_path}")
                except Exception as e:
                    logger.error(f"❌ Cannot load cached ScoutReport: {e}. Run scout first.")
                    return 1
            else:
                logger.error("❌ No cached ScoutReport found. Run `kaos --auto --phase scout` first.")
                return 1

        # 6. Act Phase
        if getattr(args, "rerun_failed", False):
            logger.info("   🔄 --rerun-failed: resetting failed tasks to PENDING")

        if phase == "scout":
            logger.info("   ⏭️  Skipping Act Phase (--phase scout)")
            return 0

        # Check compatibility score
        if report.compatibility_score < 30.0 and not getattr(args, "force_act", False):
            logger.warning(
                f"   ⚠️ Compatibility quá thấp ({report.compatibility_score}%). "
                f"Bỏ qua Act Phase. Dùng --force-act để override."
            )
            logger.info(f"📋 Scout Report: {report.reasoning}")
            return 1

        logger.info("⚡ [KAOS Auto] Act Phase — executing tasks...")
        executor = container.resolve_act_executor(target_path=target_path)
        results = await executor.execute(report=report)

        # 7. Summary
        success_count = sum(1 for r in results if r.success)
        total_count = len(results)
        elapsed = time.time() - start_time

        logger.info(
            f"\n{'='*50}\n"
            f"🏁 [KAOS Auto] Pipeline complete in {elapsed:.1f}s\n"
            f"   Tasks: {success_count}/{total_count} passed\n"
            f"   Module: {module}\n"
            f"   Compatibility: {report.compatibility_score}%\n"
            f"   Confidence: {report.confidence_level}\n"
            f"{'='*50}"
        )

        for r in results:
            status_icon = "✅" if r.success else "❌"
            logger.info(f"   {status_icon} [{r.task_id}] attempts={r.attempts}, escalated={r.escalated}")
            if r.files_created:
                logger.info(f"      Created: {', '.join(r.files_created)}")
            if r.files_modified:
                logger.info(f"      Modified: {', '.join(r.files_modified)}")

        # 8. Git Commit & Push
        if git_branch and getattr(args, "git_auto", True):
            if git_mgr is None:
                git_mgr = container.resolve_git_auto_manager(target_path=target_path)
            logger.info("📤 [KAOS Auto] Committing and pushing changes...")
            commit_ok, commit_msg = await git_mgr.commit_and_push(
                branch_name=git_branch,
                results=results,
                module=module,
            )
            if commit_ok and commit_msg != "no-changes":
                logger.info(f"   ✅ Committed: {commit_msg[:80]}...")
                logger.info(f"   🌐 Push to create PR: origin/{git_branch}")

            await git_mgr.finalize(original_branch="main")

        return 0 if all(r.success for r in results) else 1
