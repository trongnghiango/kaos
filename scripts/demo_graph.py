#!/usr/bin/env python3
import asyncio
import os
import sys
from pathlib import Path

# Add src to pythonpath
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Nạp file .env thủ công TRƯỚC KHI import bất kỳ module nào của kaos
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line_str = line.strip()
            if line_str and not line_str.startswith("#"):
                if line_str.startswith("export "):
                    line_str = line_str[7:]
                key, val = line_str.split("=", 1)
                os.environ[key.strip()] = val.strip('"\' ')

from kaos.infrastructure.di import Container
from kaos.engine.task_queue_engine import Task, TaskQueueEngine

async def run_demo():
    print("🚀 [Demo] Khởi tạo KAOS Container & Redis Graph...")
    print(f"   ├─ Env TELEGRAM_MONITOR_ENABLED: {os.getenv('TELEGRAM_MONITOR_ENABLED')}")
    print(f"   ├─ Env TELEGRAM_TOKEN: {os.getenv('TELEGRAM_TOKEN')[:6] if os.getenv('TELEGRAM_TOKEN') else None}...")
    print(f"   └─ Env TELEGRAM_CHAT_ID: {os.getenv('TELEGRAM_CHAT_ID')}")
    container = Container(target_module="demo")
    kg = container.knowledge_graph

    # Khởi động telegram session
    notification = container.telegram
    if notification:
        print("📲 [Demo] Đã phát hiện cấu hình Telegram, kích hoạt gửi thông báo...")
        await notification.__aenter__()

    # Reset graph cũ
    print("🧹 [Demo] Resetting existing graph...")
    await kg.delete_graph()

    # Khởi tạo engine
    engine = TaskQueueEngine(
        module="demo",
        llm_provider=container.llm_adapter,
        gatekeeper=container.gatekeeper_adapter,
        storage=container.storage_adapter,
        knowledge_graph=kg,
        notification=notification,
    )

    # Định nghĩa 3 Tasks (Nhân) có quan hệ phụ thuộc (DAG)
    print("\n📦 [Demo] Tạo 3 Tasks với các phụ thuộc:")
    t1 = Task(
        task_id="T1_Stub",
        module="api",
        title="Generate API user stub",
        description="Tạo file api/user.py chứa routing CRUD cơ bản",
        status="PENDING"
    )
    t2 = Task(
        task_id="T2_DB",
        module="db",
        title="Add User DB migration",
        description="Viết migration SQL thêm bảng users",
        depends_on=["T1_Stub"],
        status="PENDING"
    )
    t3 = Task(
        task_id="T3_Test",
        module="test",
        title="Add User Unit tests",
        description="Viết test cases test API user endpoint bằng pytest",
        depends_on=["T2_DB"],
        status="PENDING"
    )

    engine.tasks = {t.task_id: t for t in [t1, t2, t3]}

    # 1. Đưa thông tin Tasks & Conditions tĩnh vào Graph (Duyên)
    print("📤 [Demo] Lưu Tasks và static Conditions vào graph...")
    for t in engine.tasks.values():
        ctx = engine._build_task_context(t)
        await engine._upsert_task_context(t, ctx)

    # 2. Tính levels bằng Graph (DAG Level Calculation)
    print("📐 [Demo] Tính levels topological bằng RedisGraph...")
    await engine._calculate_levels()
    for level, tasks in sorted(engine.level_groups.items()):
        print(f"   └─ Level {level}: {[t.task_id for t in tasks]}")

    # 3. Giả lập quá trình chạy AutoFixer cho T1_Stub (Có lỗi -> Sửa -> Thành công)
    print("\n🔧 [Demo] Giả lập AutoFixer loop cho T1_Stub:")
    
    if notification:
        await notification.send_message("⏳ [Demo] Bắt đầu thực thi Task T1_Stub: Generate API user stub")

    # Attempt 1: Thất bại do lint error
    print("   ❌ Attempt 1: Lỗi linter (unused import)")
    await engine._upsert_attempt(
        task_id="T1_Stub",
        attempt=1,
        success=False,
        files_created=["api/user.py"],
        files_modified=[],
        error_msg="api/user.py:3:1: F401 'os' imported but unused",
        feedback_msg=""
    )
    if notification:
        await notification.send_alert(
            "Task T1_Stub - Lỗi Lint",
            "Attempt 1: api/user.py:3:1: F401 'os' imported but unused",
            level="WARNING"
        )

    # Attempt 2: Thành công sau khi fix
    print("   ✅ Attempt 2: Sửa thành công")
    await engine._upsert_attempt(
        task_id="T1_Stub",
        attempt=2,
        success=True,
        files_created=["api/user.py"],
        files_modified=[],
        error_msg="",
        feedback_msg="F401 'os' imported but unused -> Removed unused import"
    )
    if notification:
        await notification.send_message("✅ [Demo] Task T1_Stub thành công sau 2 attempts!")

    # 4. In ra thông số graph hiện tại
    stats = await kg.get_graph_stats()
    print(f"\n📊 [Demo] Thống kê Graph hiện tại:")
    print(f"   ├─ Tasks (Nhân): {stats['tasks']}")
    print(f"   ├─ Conditions (Duyên): {stats['conditions']}")
    print(f"   ├─ Results (Quả): {stats['results']}")
    print(f"   └─ Edges (Mối liên kết): {stats['edges']}")

    if notification:
        print("⏳ Đang đợi 3 giây để đảm bảo Telegram hoàn tất việc gửi tin nhắn...")
        await asyncio.sleep(3)
        await notification.__aexit__(None, None, None)

    print("\n🖥️ [RedisInsight Visualization Helper]")
    print("1. Hãy đảm bảo container Redis Stack đang chạy.")
    print("2. Mở trình duyệt truy cập: http://localhost:8001")
    print("3. Chọn database của bạn (localhost:6380 nếu chạy docker-compose của KAOS).")
    print("4. Vào mục Graph visualizer hoặc chạy các lệnh Cypher sau để query cấu trúc Nhân-Duyên-Quả:")
    print("   - Xem toàn bộ Tasks & Dependency DAG:")
    print("     MATCH (t:Task) OPTIONAL MATCH (t)-[r:DEPENDS_ON]->(d:Task) RETURN t, r, d")
    print("\n   - Xem lịch sử thực thi AutoFixer (Quả):")
    print("     MATCH (t:Task {task_id: 'T1_Stub'})-[:PRODUCES]->(r:Result) RETURN r.attempt, r.success, r.error_message")
    print("\n   - Xem các Duyên động (Feedback) sinh ra từ các lần sửa lỗi:")
    print("     MATCH (t:Task)<-[:REQUIRES]-(c:Condition {type: 'feedback'}) RETURN t.task_id, c.content")
    print("\n🎉 Demo hoàn thành! Giữ dữ liệu trên Redis để bạn có thể visualize.")

if __name__ == "__main__":
    asyncio.run(run_demo())
