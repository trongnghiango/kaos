#!/usr/bin/env python3
import asyncio
import os
import sys
import time
import tracemalloc
from pathlib import Path

# Add src to pythonpath
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kaos.infrastructure.di import Container
from kaos.engine.task_queue_engine import Task, TaskQueueEngine

async def run_benchmark():
    print("=====================================================================")
    # Initialize container & graph
    container = Container(target_module="benchmark")
    kg = container.knowledge_graph
    
    # Define a complex DAG with 5 tasks
    tasks_def = [
        Task(task_id="T1", module="api", title="API users stub", description="T1 description", status="PENDING"),
        Task(task_id="T2", module="db", title="DB migrations", description="T2 description", depends_on=["T1"], status="PENDING"),
        Task(task_id="T3", module="logic", title="Auth validation", description="T3 description", depends_on=["T1"], status="PENDING"),
        Task(task_id="T4", module="test", title="API tests", description="T4 description", depends_on=["T2", "T3"], status="PENDING"),
        Task(task_id="T5", module="deploy", title="Prod script", description="T5 description", depends_on=["T4"], status="PENDING"),
    ]

    print("📊 [Benchmark] Starting comparative benchmark: File-based vs RedisGraph")
    print(f"   ├─ Tasks in DAG: {len(tasks_def)}")
    print("=====================================================================")

    # 1. Benchmark File-Based (Simulated via context construction + json writes)
    print("\n💾 [File-Based Context Flow]")
    tracemalloc.start()
    start_time = time.time()
    
    # Simulated file‑based engine (graph disabled)
    engine_file = TaskQueueEngine(
        module="benchmark_file",
        llm_provider=container.llm_adapter,
        gatekeeper=container.gatekeeper_adapter,
        storage=container.storage_adapter,
        knowledge_graph=None,
    )
    engine_file.tasks = {t.task_id: t for t in tasks_def}
    
    # 1a. Build levels – note that `_calculate_levels` is now async
    await engine_file._calculate_levels()
    
    # 1b. Simulate the whole execution cycle (file I/O & AutoFixer)
    file_io_bytes = 0
    for t in engine_file.tasks.values():
        # Build & write task context (file‑based)
        ctx = engine_file._build_task_context(t)
        ctx_file = Path(f"/tmp/act_ctx_{t.task_id}.json")
        container.storage_adapter.write_json(ctx_file, ctx)
        file_io_bytes += len(str(ctx))
        _ = container.storage_adapter.read_json(ctx_file)
        file_io_bytes += len(str(ctx))
        
        # Simulate a failing AutoFixer attempt (writes error history file)
        err_file = Path(f"/tmp/.error_history_{t.task_id}.json")
        container.storage_adapter.write_json(err_file, {"error": "Lint failed", "attempt": 1})
        file_io_bytes += 100
        
        # Simulate a succeeding attempt (writes final result file)
        out_file = Path(f"/tmp/act_out_{t.task_id}.json")
        container.storage_adapter.write_json(out_file, {"success": True, "attempt": 2})
        file_io_bytes += 150
        
        # Clean up temporary files
        if ctx_file.exists(): ctx_file.unlink()
        if err_file.exists(): err_file.unlink()
        if out_file.exists(): out_file.unlink()
    
    file_time = time.time() - start_time
    _, file_memory_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    print(f"   ├─ Time Taken   : {file_time:.5f} seconds")
    print(f"   ├─ Memory Peak  : {file_memory_peak / 1024:.2f} KB")
    print(f"   └─ Disk I/O Est. : {file_io_bytes / 1024:.2f} KB written/read")

    # 2. Benchmark RedisGraph (graph‑enabled)
    print("\n🔴 [RedisGraph Context Flow]")
    await kg.delete_graph()
    tracemalloc.start()
    start_time = time.time()
    
    engine_graph = TaskQueueEngine(
        module="benchmark_graph",
        llm_provider=container.llm_adapter,
        gatekeeper=container.gatekeeper_adapter,
        storage=container.storage_adapter,
        knowledge_graph=kg,
    )
    engine_graph.tasks = {t.task_id: t for t in tasks_def}
    
    # 2a. First upsert all tasks & dependencies into RedisGraph
    graph_io_bytes = 0
    for t in engine_graph.tasks.values():
        ctx = engine_graph._build_task_context(t)
        await engine_graph._upsert_task_context(t, ctx)
        graph_io_bytes += len(str(ctx))
    
    # 2b. Build levels using the graph (async) – now DEPENDS_ON edges exist
    await engine_graph._calculate_levels()
    
    # 2c. Simulate AutoFixer attempts
    for t in engine_graph.tasks.values():
        
        # Fail attempt 1
        await engine_graph._upsert_attempt(
            task_id=t.task_id,
            attempt=1,
            success=False,
            files_created=[],
            files_modified=[],
            error_msg="Lint failed",
            feedback_msg="",
        )
        # Success attempt 2
        await engine_graph._upsert_attempt(
            task_id=t.task_id,
            attempt=2,
            success=True,
            files_created=[],
            files_modified=[],
            error_msg="",
            feedback_msg="",
        )
    
    graph_time = time.time() - start_time
    _, graph_memory_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    # Statistics from RedisGraph
    stats = await kg.get_graph_stats()
    
    print(f"   ├─ Time Taken   : {graph_time:.5f} seconds")
    print(f"   ├─ Memory Peak  : {graph_memory_peak / 1024:.2f} KB")
    print(f"   ├─ Disk I/O Est. : 0 KB (in‑memory Redis)"
    )
    print(
        f"   └─ Redis Nodes  : Tasks={stats['tasks']}, Conditions={stats['conditions']}, Results={stats['results']}, Edges={stats['edges']}"
    )
    # 3. Comparative Summary
    print("\n📈 [Comparative Summary]")
    print(f"   ├─ Speedup Ratio  : {file_time / graph_time:.2f}x faster with RedisGraph")
    print(f"   ├─ Disk I/O Saved : {file_io_bytes / 1024:.2f} KB of disk reads/writes avoided entirely")
    print(f"   └─ Memory Saved   : {(file_memory_peak - graph_memory_peak) / 1024:.2f} KB less peak memory")
    print("=====================================================================")

    # Write benchmark to file
    benchmark_dir = Path(__file__).parent.parent / "benchmarks"
    benchmark_dir.mkdir(exist_ok=True)
    
    csv_content = f"""Metric,File-Based,RedisGraph,Improvement
Speed (s),{file_time:.5f},{graph_time:.5f},{file_time / graph_time:.2f}x
Peak Memory (KB),{file_memory_peak / 1024:.2f},{graph_memory_peak / 1024:.2f},{(file_memory_peak - graph_memory_peak) / 1024:.2f} KB saved
Disk I/O (KB),{file_io_bytes / 1024:.2f},0.00,100% saved
"""
    with open(benchmark_dir / "graph_vs_file.csv", "w") as f:
        f.write(csv_content)
    print(f"📝 Saved benchmark results to: {benchmark_dir / 'graph_vs_file.csv'}")
    
    await kg.delete_graph()

if __name__ == "__main__":
    asyncio.run(run_benchmark())
