"""
File Storage Adapter implementing StoragePort
============================================
Thao tác với hệ thống tệp tin local (đọc/ghi file CSV, JSON, text).
"""

import csv
import json
import logging
from pathlib import Path
from typing import Dict

from kaos.application.ports import StoragePort
from kaos.domain.models import Task

logger = logging.getLogger("STAX_Harness")


class FileStorageAdapter(StoragePort):
    """Triển khai StoragePort trực tiếp trên Local Filesystem"""

    def load_queue_tasks(self, csv_path: Path, default_module: str, resume: bool = False) -> Dict[str, Task]:
        if not csv_path.exists():
            raise FileNotFoundError(f"Không tìm thấy file queue: {csv_path}")

        tasks: Dict[str, Task] = {}

        with open(csv_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            delimiter = "\t" if "\t" in first_line else ","
            f.seek(0)

            reader = csv.DictReader(f, delimiter=delimiter)
            required_cols = {"task_id", "title", "description"}
            if not required_cols.issubset(reader.fieldnames):
                raise ValueError(
                    f"File CSV thiếu các cột bắt buộc: {required_cols}. "
                    f"Cột hiện tại: {reader.fieldnames}"
                )

            for row in reader:
                task_id = row["task_id"].strip()
                depends_raw = row.get("depends_on", "").strip()
                depends = [d.strip() for d in depends_raw.split(",") if d.strip()]
                status = row.get("status", "PENDING").strip()

                task = Task(
                    task_id=task_id,
                    module=row.get("module", default_module).strip(),
                    title=row["title"].strip(),
                    description=row["description"].strip(),
                    depends_on=depends,
                    status=status,
                )
                
                # Nếu resume và task đã SUCCESS, giữ nguyên trạng thái SUCCESS
                if resume and status == "SUCCESS":
                    task.status = "SUCCESS"
                
                tasks[task_id] = task

        logger.info(f"📂 [KAOS Storage] Đã tải {len(tasks)} tasks từ file {csv_path.name}")
        return tasks

    def save_queue_status(self, csv_path: Path, tasks: Dict[str, Task]) -> None:
        if not csv_path.exists():
            logger.warning(f"⚠️ Không tìm thấy file CSV gốc: {csv_path} để cập nhật trạng thái.")
            return

        try:
            rows = []
            fieldnames = []
            
            # Đọc lại file cũ để giữ nguyên các cột phụ khác (nếu có)
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    task_id = row.get("task_id")
                    if task_id in tasks:
                        row["status"] = tasks[task_id].status
                    rows.append(row)

            # Ghi đè lại
            with open(csv_path, mode="w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
                
            logger.debug(f"💾 [KAOS Storage] Đã lưu trạng thái tasks vào: {csv_path.name}")
        except Exception as e:
            logger.error(f"⚠️ [KAOS Storage] Lỗi lưu file CSV status: {e}")

    def write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def read_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def delete_file(self, path: Path) -> None:
        if path.exists():
            path.unlink()

    def read_text(self, path: Path) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def file_exists(self, path: Path) -> bool:
        return path.exists()