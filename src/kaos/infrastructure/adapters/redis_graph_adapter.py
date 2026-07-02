"""
RedisGraph Adapter for KAOS Knowledge Graph
============================================
Triển khai KnowledgeGraphPort bằng Redis Hashes + Sets (không cần module RedisGraph).
Dữ liệu được lưu dưới dạng:

  - `kg:task:{task_id}` -- Hash (task properties)
  - `kg:condition:{cond_id}` -- Hash (condition properties)
  - `kg:result:{result_id}` -- Hash (result properties)
  - `kg:edge:{node}:requires` -- Set of condition IDs (REQUIRES)
  - `kg:edge:{node}:produces` -- Set of result IDs (PRODUCES)
  - `kg:edge:{result}:mutates` -- Set of condition IDs (MUTATES)
  - `kg:edge:{child}:depends_on` -- Set of parent task IDs (DEPENDS_ON)
  - `kg:idx:tasks` -- Set of all task_id
  - `kg:idx:conditions` -- Set of all cond_id
  - `kg:idx:results` -- Set of all result_id

Các phương thức bất đồng bộ (async) sử dụng `asyncio.to_thread()` vì redis-py là sync.
"""

import asyncio
import json
import logging

import redis

from kaos.application.ports import KnowledgeGraphPort

logger = logging.getLogger(__name__)

# Graph key name
GRAPH_NAME = "KNOWLEDGE"  # used for RedisGraph compatibility naming

# Default Redis connection params
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 6380
DEFAULT_DB = 0


class RedisGraphAdapter(KnowledgeGraphPort):
    """
    Redis-backed implementation of KnowledgeGraphPort.

    Uses Hash/Sets for O(1) node lookups and efficient edge traversal,
    without requiring the deprecated RedisGraph module.

    Compatible with Redis 6+ (any Redis instance, no module dependency).
    """

    def __init__(
        self, redis_url: str | None = None, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, db: int = DEFAULT_DB
    ):
        if redis_url:
            self.client = redis.from_url(redis_url, decode_responses=True)
        else:
            self.client = redis.Redis(
                host=host,
                port=port,
                db=db,
                decode_responses=True,
            )
        logger.info(
            "RedisGraphAdapter initialized — connected to %s:%s/%s",
            host,
            port,
            db,
        )

    # ──────────────────────────────────────────────────────
    #  KEY HELPERS
    # ──────────────────────────────────────────────────────

    def _task_key(self, task_id: str) -> str:
        return f"kg:task:{task_id}"

    def _cond_key(self, cond_id: str) -> str:
        return f"kg:condition:{cond_id}"

    def _result_key(self, result_id: str) -> str:
        return f"kg:result:{result_id}"

    def _edge_key(self, node_id: str, edge_type: str) -> str:
        """edge_type = requires | produces | mutates | depends_on"""
        return f"kg:edge:{node_id}:{edge_type}"

    def _idx_key(self, idx_name: str) -> str:
        return f"kg:idx:{idx_name}"

    # ──────────────────────────────────────────────────────
    #  NODE OPERATIONS
    # ──────────────────────────────────────────────────────

    async def upsert_task(
        self,
        task_id: str,
        title: str = "",
        description: str = "",
        module: str = "",
        complexity: str = "MEDIUM",
        status: str = "PENDING",
    ) -> bool:
        def _sync():
            key = self._task_key(task_id)
            self.client.hset(
                key,
                mapping={
                    "task_id": task_id,
                    "title": title,
                    "description": description,
                    "module": module,
                    "complexity": complexity,
                    "status": status,
                },
            )
            # Also track in index
            self.client.sadd(self._idx_key("tasks"), task_id)
            return True

        return await asyncio.to_thread(_sync)

    async def upsert_condition(self, cond_id: str, cond_type: str, content: str, hash_val: str = "") -> str:
        def _sync():
            key = self._cond_key(cond_id)
            self.client.hset(
                key,
                mapping={
                    "cond_id": cond_id,
                    "type": cond_type,
                    "content": content,
                    "hash": hash_val,
                },
            )
            self.client.sadd(self._idx_key("conditions"), cond_id)
            return cond_id

        return await asyncio.to_thread(_sync)

    async def upsert_result(
        self,
        result_id: str,
        task_id: str,
        success: bool,
        files_created: list,
        files_modified: list,
        error_message: str = "",
        attempt: int = 1,
    ) -> str:
        def _sync():
            key = self._result_key(result_id)
            self.client.hset(
                key,
                mapping={
                    "result_id": result_id,
                    "task_id": task_id,
                    "success": str(success).lower(),
                    "files_created": json.dumps(files_created),
                    "files_modified": json.dumps(files_modified),
                    "error_message": error_message,
                    "attempt": str(attempt),
                },
            )
            self.client.sadd(self._idx_key("results"), result_id)

            # Link Task → Result (PRODUCES)
            self.client.sadd(self._edge_key(task_id, "produces"), result_id)

            # Reverse: result → task
            self.client.hset("kg:rev:result_task", result_id, task_id)

            return result_id

        return await asyncio.to_thread(_sync)

    # ──────────────────────────────────────────────────────
    #  EDGE OPERATIONS
    # ──────────────────────────────────────────────────────

    async def link_task_condition(self, task_id: str, cond_id: str) -> bool:
        def _sync():
            # Task requires Condition
            self.client.sadd(self._edge_key(task_id, "requires"), cond_id)
            # Condition belongs to Task (reverse)
            self.client.sadd(self._edge_key(cond_id, "required_by"), task_id)
            return True

        return await asyncio.to_thread(_sync)

    async def link_result_condition(self, result_id: str, cond_id: str) -> bool:
        def _sync():
            self.client.sadd(self._edge_key(result_id, "mutates"), cond_id)
            return True

        return await asyncio.to_thread(_sync)

    async def link_task_dependency(self, parent_id: str, child_id: str) -> bool:
        """child_id depends_on parent_id (DAG edge: child -> parent)."""

        def _sync():
            self.client.sadd(self._edge_key(child_id, "depends_on"), parent_id)
            return True

        return await asyncio.to_thread(_sync)

    # ──────────────────────────────────────────────────────
    #  QUERY OPERATIONS
    # ──────────────────────────────────────────────────────

    async def get_task(self, task_id: str) -> dict | None:
        def _sync():
            key = self._task_key(task_id)
            if not self.client.exists(key):
                return None
            data = self.client.hgetall(key)
            return data

        return await asyncio.to_thread(_sync)

    async def get_task_results(self, task_id: str) -> list:
        def _sync():
            res_ids = self.client.smembers(self._edge_key(task_id, "produces"))
            results = []
            for rid in res_ids:
                data = self.client.hgetall(self._result_key(rid))
                if data:
                    # Parse serialized fields
                    for field in ("files_created", "files_modified"):
                        if field in data:
                            try:
                                data[field] = json.loads(data[field])
                            except (json.JSONDecodeError, TypeError):
                                pass
                    if "success" in data:
                        data["success"] = data["success"] == "true"
                    if "attempt" in data:
                        try:
                            data["attempt"] = int(data["attempt"])
                        except ValueError:
                            pass
                    results.append(data)
            # Sort by attempt descending
            results.sort(key=lambda r: int(r.get("attempt", 0)), reverse=True)
            return results

        return await asyncio.to_thread(_sync)

    async def get_last_latest_result(self, task_id: str) -> dict | None:
        results = await self.get_task_results(task_id)
        if not results:
            return None
        return results[0]  # sorted by attempt desc

    async def get_conditions_by_type(self, cond_type: str) -> list:
        def _sync():
            all_cond_ids = self.client.smembers(self._idx_key("conditions"))
            matched = []
            for cid in all_cond_ids:
                data = self.client.hgetall(self._cond_key(cid))
                if data and data.get("type") == cond_type:
                    matched.append(data)
            return matched

        return await asyncio.to_thread(_sync)

    async def get_task_dependencies(self, task_id: str) -> list:
        def _sync():
            parents = self.client.smembers(self._edge_key(task_id, "depends_on"))
            return list(parents)

        return await asyncio.to_thread(_sync)

    async def calculate_levels(self) -> dict:
        """
        BFS topological sort based on DEPENDS_ON edges.
        Returns { "levels": {0: [task_id, ...], 1: [...], ...}, "max_level": int }
        """

        def _sync():
            all_task_ids = self.client.smembers(self._idx_key("tasks"))
            in_degree = {}
            children_map: dict[str, list[str]] = {}

            for tid in all_task_ids:
                in_degree[tid] = 0
                children_map[tid] = []

            for child_id in all_task_ids:
                parents = self.client.smembers(self._edge_key(child_id, "depends_on"))
                for parent_id in parents:
                    if parent_id in all_task_ids:
                        in_degree[child_id] += 1
                        children_map.setdefault(parent_id, []).append(child_id)

            queue = [tid for tid, deg in in_degree.items() if deg == 0]
            levels: dict[int, list] = {}
            current_level = 0

            while queue:
                next_queue = []
                for tid in queue:
                    if current_level not in levels:
                        levels[current_level] = []
                    levels[current_level].append(tid)
                    for child in children_map.get(tid, []):
                        in_degree[child] -= 1
                        if in_degree[child] == 0:
                            next_queue.append(child)
                queue = next_queue
                current_level += 1

            # Remaining tasks (cycles)
            remaining = [tid for tid, deg in in_degree.items() if deg > 0]
            if remaining:
                levels[current_level] = remaining
                current_level += 1

            return {
                "levels": levels,
                "max_level": max(levels.keys()) if levels else 0,
            }

        return await asyncio.to_thread(_sync)

    async def get_all_tasks(self) -> list:
        def _sync():
            task_ids = self.client.smembers(self._idx_key("tasks"))
            tasks = []
            for tid in task_ids:
                data = self.client.hgetall(self._task_key(tid))
                if data:
                    tasks.append(data)
            return tasks

        return await asyncio.to_thread(_sync)

    async def delete_graph(self) -> bool:
        def _sync():
            for key in self.client.scan_iter(match="kg:*"):
                self.client.delete(key)
            logger.info("Knowledge Graph deleted (all kg:* keys cleared).")
            return True

        return await asyncio.to_thread(_sync)

    async def get_graph_stats(self) -> dict:
        def _sync():
            tasks = self.client.scard(self._idx_key("tasks"))
            conds = self.client.scard(self._idx_key("conditions"))
            results = self.client.scard(self._idx_key("results"))
            # Count edges
            edge_count = 0
            for key in self.client.scan_iter(match="kg:edge:*"):
                edge_count += self.client.scard(key)
            return {
                "tasks": tasks,
                "conditions": conds,
                "results": results,
                "edges": edge_count,
            }

        return await asyncio.to_thread(_sync)
