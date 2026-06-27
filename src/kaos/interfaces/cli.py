"""
CLI Interface for KAOS (Clean Architecture Orchestrator)
========================================================
Nhận lệnh từ Terminal, cấu hình DI container, điều phối các Use Cases.
Thay thế và bao bọc toàn bộ logic của smart_orchestrator cũ một cách sạch sẽ.
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path


def show_status(output_csv: Path, container):
    """In trạng thái hiện tại của Task Queue"""
    from kaos.config import logger
    if output_csv and output_csv.exists():
        logger.info(f"\n📊 [KAOS Status] Task Queue: {output_csv.name}")
        try:
            tasks = container.storage_adapter.load_queue_tasks(
                output_csv, container.target_module, resume=True
            )
            for tid, task in tasks.items():
                logger.info(f"   - [{task.status}] {task.task_id}: {task.title}")
        except Exception as e:
            logger.error(f"❌ Không thể đọc trạng thái task queue: {e}")
    else:
        logger.warning("⚠️ Chưa có Task Queue CSV được tạo.")


async def run_pipeline(args) -> int:
    from kaos.config import TARGET_PATH, TMP_DIR, logger
    from kaos.infrastructure.di import Container

    start_time = time.time()

    # Lấy spec và raw_data riêng biệt và giải quyết đường dẫn tương đối từ CWD của process hoặc TARGET_PATH
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

    # Nếu `--status` hoặc `--rerun-failed` được bật mà chưa truyền gì, mock = dummy
    if args.status or args.rerun_failed:
        if not raw_data_path and not spec_input:
            raw_data_path = "dummy"

    if not raw_data_path and not spec_input:
        logger.error("❌ Thiếu đầu vào: cần ít nhất raw_data (file Excel/CSV) hoặc --spec (file/chuỗi yêu cầu).")
        return 1

    # TỰ ĐỘNG ĐƠN GIẢN HÓA:
    # Nếu có đầu vào (raw_data hoặc spec) mà KHÔNG chỉ định rõ chạy sửa code (--resume / --rerun-failed hoặc --phase là execute)
    # Thì tự động chuyển sang chế độ Phân tích tương thích và Ra quyết định tối ưu (Dry-Run) xuất báo cáo ra thư mục hiện hành.
    is_execution_mode = args.resume or args.rerun_failed or args.phase == "execute"
    if not is_execution_mode and not args.compatibility_report:
        args.compatibility_report = str(Path.cwd() / "db_compatibility_report.md")
        args.run_dry = True
        logger.info(f"💡 [KAOS Orchestrator] Tự động kích hoạt chế độ Phân tích tương thích & Ra quyết định tối ưu (Dry-Run)")
        logger.info(f"   Báo cáo quyết định sẽ xuất ra: {args.compatibility_report}")

    # 1. Tự động nhận diện module và scope nếu set --module auto
    detected_scope = None
    if args.module == "auto":
        # Khởi tạo container tạm để phân tích scope
        temp_container = Container(target_module="system", branch_name=args.branch)
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

    # 2. Xác định file CSV hàng đợi cho module cụ thể
    tmp_dir = TMP_DIR
    output_csv = tmp_dir / f"goose_out_data_analyzer_{args.module}.csv"

    # 3. Khởi tạo DI Container chính thức cho module đích
    container = Container(
        target_module=args.module,
        branch_name=args.branch,
        llm_provider=getattr(args, "llm_provider", None),
    )

    # 4. Xem trạng thái
    if args.status:
        show_status(output_csv, container)
        return 0

    # 4.5. Phân tích độ tương thích database cũ và sinh báo cáo (Dry-Run)
    if args.compatibility_report or args.run_dry:
        report_path = args.compatibility_report or "tools/kaos/tmp/db_compatibility_report.md"
        report_path_obj = Path(report_path)
        if not report_path_obj.is_absolute():
            if args.compatibility_report:
                report_path = str((Path.cwd() / report_path_obj).resolve())
            else:
                from kaos.config import TARGET_PATH
                report_path = str((TARGET_PATH / "tools" / "kaos" / "tmp" / "db_compatibility_report.md").resolve())
        else:
            report_path = str(report_path_obj.resolve())
        if not raw_data_path:
            logger.error("❌ Phân tích độ tương thích database yêu cầu đầu vào raw_data (đường dẫn file Excel .xlsx).")
            return 1
        
        # Validate raw_data path exists
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

    # 5. Trích xuất Schema
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

    # 6. Phân tích yêu cầu (Analyze)
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

    # 7. Thực thi các Task (Execute)
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


async def run_auto_pipeline(args) -> int:
    """
    Scout→Act Auto Pipeline: Scout → Synthesizer → Act + AutoFixer.

    Extended flags from Sprint 3:
      --phase scout|act|all (default: all)
      --resume             skip already-successful tasks
      --rerun-failed       reset failed tasks back to PENDING
      --status             show pipeline status and exit
      --parallel N         max parallel workers (passed to engine)
    """
    import time
    from kaos.config import TARGET_PATH, TMP_DIR, logger
    from kaos.infrastructure.di import Container
    from kaos.engine.task_queue_engine import TaskQueueEngine

    start_time = time.time()
    logger.info("🤖 [KAOS Auto] Scout→Act Pipeline started")

    # ── --status support: show current engine task state ──────
    if getattr(args, "status", False):
        status_path = TMP_DIR / "engine_status.json"
        if status_path.exists():
            import json
            try:
                data = json.loads(status_path.read_text())
                logger.info(f"\n📊 [KAOS Auto] Engine Status:")
                logger.info(f"   Branch : {data.get('branch_name', 'N/A')}")
                logger.info(f"   Tasks  : {data.get('total', 0)} total, "
                            f"{data.get('completed', 0)} completed, "
                            f"{data.get('failed', 0)} failed")
                for t in data.get("tasks", []):
                    logger.info(f"   - [{t.get('status','?')}] {t.get('task_id','')}: {t.get('title','')[:60]}")
            except Exception as e:
                logger.error(f"❌ Cannot read engine status: {e}")
        else:
            logger.warning("⚠️ No engine status file found.")
        return 0

    # 1. Resolve target path
    target_path = str(TARGET_PATH) if TARGET_PATH else str(Path.cwd())

    # 2. Resolve spec
    spec_input = args.spec if args.spec else None
    if spec_input:
        spec_path = Path(spec_input)
        if not spec_path.is_absolute():
            cwd_path = (Path.cwd() / spec_path).resolve()
            if cwd_path.exists():
                spec_input = str(cwd_path)
        elif spec_path.exists():
            spec_input = spec_path.read_text(encoding="utf-8")

    # 3. Resolve raw_data
    raw_data = args.raw_data if args.raw_data else None
    if raw_data:
        raw_path = Path(raw_data)
        if not raw_path.is_absolute():
            cwd_raw = (Path.cwd() / raw_path).resolve()
            if cwd_raw.exists():
                raw_data = str(cwd_raw)

    # 4. Auto-detect module nếu là "auto"
    module = args.module
    if module == "auto":
        temp_container = Container(target_module="system", branch_name=args.branch)
        detect_use_case = temp_container.resolve_detect_scope()
        try:
            detected_scope = await detect_use_case.execute(spec=spec_input, raw_data=raw_data)
            module = detected_scope.get("recommended_module", "all")
            logger.info(f"🎯 [KAOS Auto] Detected module: '{module}'")
        except Exception as e:
            logger.warning(f"   ⚠️ Auto-detect failed: {e}. Using fallback='all'.")
            module = "all"

    # 5. Init container
    container = Container(
        target_module=module,
        branch_name=args.branch,
        llm_provider=getattr(args, "llm_provider", None),
    )

    # 5.5. Git Auto Branch (Mode B)
    phase = getattr(args, "phase", "all")
    git_branch = ""
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

    # 6. Scout Phase (skip if --phase act)
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

        # If --phase scout, exit after scout
        if phase == "scout":
            elapsed = time.time() - start_time
            logger.info(f"\n🏁 [KAOS Auto] Scout-only phase complete in {elapsed:.1f}s")
            return 0
    else:
        # --phase act: load cached ScoutReport from file
        from kaos.domain.scout_results import ScoutReport
        cached_report_path = TMP_DIR / "scout_report.json"
        if cached_report_path.exists():
            import json
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

    # 7. Act Phase
    if getattr(args, "rerun_failed", False):
        logger.info("   🔄 --rerun-failed: resetting failed tasks to PENDING")

    if phase == "scout":
        logger.info("   ⏭️  Skipping Act Phase (--phase scout)")
        return 0

    # Check compatibility threshold
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

    # 8. Summary
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

    # 9. Git Auto Commit + Push
    if git_branch and getattr(args, "git_auto", True):
        logger.info("📤 [KAOS Auto] Committing and pushing changes...")
        commit_ok, commit_msg = await git_mgr.commit_and_push(
            branch_name=git_branch,
            results=results,
            module=module,
        )
        if commit_ok and commit_msg != "no-changes":
            logger.info(f"   ✅ Committed: {commit_msg[:80]}...")
            logger.info(f"   🌐 Push to create PR: origin/{git_branch}")

        # Finalize: checkout về main
        await git_mgr.finalize(original_branch="main")

    return 0 if all(r.success for r in results) else 1


def main():
    # 0. Tiền xử lý cờ --target-path để thiết lập môi trường trước khi các module khác import config
    target_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--target-path" and i + 1 < len(sys.argv):
            target_path = sys.argv[i + 1]
            break
        elif arg.startswith("--target-path="):
            target_path = arg.split("=", 1)[1]
            break

    if target_path:
        os.environ["KAOS_TARGET_PATH"] = str(Path(target_path).resolve())

    # Bây giờ mới import config và logger an toàn
    from kaos.config import logger

    parser = argparse.ArgumentParser(description="KAOS Clean Architecture Standalone Orchestrator")
    parser.add_argument("raw_data", nargs="?", help="Đường dẫn đến file Excel/CSV/TSV/Document (.md,.txt) thô")
    parser.add_argument(
        "--spec",
        help="Chuỗi Spec trực tiếp (dùng thay raw_data khi không có file). Ví dụ: --spec \"Tạo API CRUD cho CRM Contact\"",
    )
    parser.add_argument(
        "--module",
        default="auto",
        help="Module đích (vd: crm, accounting). Mặc định 'auto' để LLM tự nhận diện module phù hợp từ spec.",
    )
    parser.add_argument(
        "--target-path",
        help="Đường dẫn tuyệt đối đến thư mục dự án đích (Target Codebase)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Tiếp tục chạy tiếp từ trạng thái lưu ở file CSV lần trước (bỏ qua SUCCESS)",
    )
    parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="Đặt lại trạng thái của các task FAILED về PENDING và chạy lại",
    )
    parser.add_argument(
        "--phase",
        choices=["all", "extract", "analyze", "execute"],
        default="all",
        help="Chạy một phase cụ thể trong pipeline",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="In trạng thái task queue hiện tại và thoát",
    )
    parser.add_argument(
        "--branch",
        help="Tên nhánh Git cách ly (nếu để trống sẽ tự động tạo dựa trên module và session)",
    )
    parser.add_argument(
        "--compatibility-report",
        help="Đường dẫn đến file Markdown (.md) để xuất báo cáo độ tương thích của database cũ/yêu cầu khách hàng đối với codebase hiện tại (Chế độ dry-run không làm thay đổi code)",
    )
    parser.add_argument(
        "--run-dry",
        action="store_true",
        help="Kích hoạt chế độ dry-run để sinh báo cáo độ tương thích mà không thay đổi bất kỳ dòng code nào. Báo cáo sẽ kèm theo đề xuất diff/patch.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=5,
        help="Số lượng tasks chạy song song tối đa (giữ lại để tương thích ngược)",
    )
    parser.add_argument(
        "--llm-provider",
        dest="llm_provider",
<<<<<<< Updated upstream
<<<<<<< Updated upstream
<<<<<<< Updated upstream
<<<<<<< Updated upstream
        choices=["goose", "claude-code", "antigravity"],
=======
        choices=["goose", "antigravity", "claude-code"],
>>>>>>> Stashed changes
=======
        choices=["goose", "antigravity", "claude-code"],
>>>>>>> Stashed changes
=======
        choices=["goose", "antigravity", "claude-code"],
>>>>>>> Stashed changes
=======
        choices=["goose", "antigravity", "claude-code"],
>>>>>>> Stashed changes
        default=None,
        help=(
            "LLM provider được dùng để thực thi task. Mặc định đọc từ KAOS_LLM_PROVIDER env "
            "hoặc runner_config.json (fallback: 'goose'). "
            "Ví dụ: --llm-provider antigravity, --llm-provider claude-code"
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Chế độ Scout→Act tự động: Scout Phase (parallel) → Synthesizer → Act Phase + AutoFixer",
    )
    parser.add_argument(
        "--force-reparse",
        action="store_true",
        help="Bypass schema cache, force re-extract trong Scout Phase",
    )
    parser.add_argument(
        "--force-act",
        action="store_true",
        help="Force chạy Act Phase kể cả khi compatibility score thấp",
    )

    args = parser.parse_args()

    if args.auto:
        sys.exit(asyncio.run(run_auto_pipeline(args)))
    else:
        sys.exit(asyncio.run(run_pipeline(args)))


if __name__ == "__main__":
    main()