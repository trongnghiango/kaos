# HANDOFF.md

## 1. User Intent
*   **Correct Spec File Routing**: Prevent `kaos` from treating the specification Markdown file `refactor-extract-packages.spec.md` as a database data file.
*   **Correct Codebase Target Path**: Ensure execution targets the `/home/ka/Repos/github.com/trongnghiango/STAX_ASP` codebase instead of `/home/ka/Repos/github.com/trongnghiango`.
*   **Fix Broken Tests**: Resolve circular import errors, missing attributes, and Git execution side-effects during `pytest` runs.
*   **Allow Spec-Only Dry-Runs**: Remove the absolute requirement of raw database Excel files (`.xlsx`) during compatibility analysis and dry-runs, allowing analysis based only on Markdown specification documents.
*   **Branch Isolation**: Work on a clean `develop` branch as the development base instead of working on `main`.

## 2. Technical Concepts
*   **Input Heuristics**: Automatically route arguments by file extension (e.g., routing `.md`, `.txt`, `.markdown` to `spec_input` instead of `raw_data_path`).
*   **Test isolation**: Use `GitPort` abstraction and mock fixtures to decouple tests from the host's actual Git repository (blocking execution of `git stash` or `git checkout` on the project root during unit tests).
*   **Fallback Topological Sort**: Fallback to in-memory DAG levels sorting in `TaskQueueEngine` when graph levels are missing or empty.
*   **Redis Causal Graph Structure**: Using Redis Sets and Hashes as a lightweight knowledge graph implementation to store Task, Condition, and Result nodes without relying on deprecated RedisGraph query engines.

## 3. Files + Code

### File: `/home/ka/Repos/github.com/trongnghiango/kaos/src/kaos/interfaces/cli.py`
Updated input mapping logic and compatibility checking:
```python
def resolve_inputs(args, target_path: Path) -> tuple[Optional[str], Optional[str]]:
    raw_input = args.raw_data
    spec_input = args.spec

    resolved_raw = None
    if raw_input:
        raw_path = Path(raw_input)
        if raw_path.is_absolute():
            if raw_path.exists():
                resolved_raw = raw_path
        else:
            cwd_raw = (Path.cwd() / raw_path).resolve()
            target_raw = (target_path / raw_path).resolve()
            if cwd_raw.exists():
                resolved_raw = cwd_raw
            elif target_raw.exists():
                resolved_raw = target_raw
            else:
                resolved_raw = raw_path

    if resolved_raw and resolved_raw.exists() and resolved_raw.is_file() and not spec_input:
        if resolved_raw.suffix.lower() in ['.md', '.txt', '.markdown']:
            spec_input = str(resolved_raw)
            raw_input = None
            resolved_raw = None

    resolved_spec = None
    if spec_input:
        spec_path = Path(spec_input)
        if spec_path.is_absolute():
            if spec_path.exists():
                resolved_spec = str(spec_path)
            else:
                resolved_spec = spec_input
        else:
            cwd_spec = (Path.cwd() / spec_path).resolve()
            target_spec = (target_path / spec_path).resolve()
            if cwd_spec.exists():
                resolved_spec = str(cwd_spec)
            elif target_spec.exists():
                resolved_spec = str(target_spec)
            else:
                resolved_spec = spec_input

    resolved_raw_str = str(resolved_raw) if resolved_raw else (raw_input if raw_input else None)
    return resolved_raw_str, resolved_spec
```

```python
# Modified block in compatibility check inside run_pipeline:
        if not raw_data_path and not spec_input:
            logger.error("❌ Phân tích độ tương thích database yêu cầu đầu vào raw_data (đường dẫn file Excel .xlsx) hoặc spec (đặc tả nghiệp vụ).")
            return 1
        
        resolved_raw_path = None
        if raw_data_path:
            resolved_raw_path = Path(raw_data_path).resolve()
            if not resolved_raw_path.exists():
                logger.error(f"❌ File raw_data không tồn tại tại: {raw_data_path}")
                return 1
```

### File: `/home/ka/Repos/github.com/trongnghiango/kaos/src/kaos/application/use_cases/analyze_compatibility.py`
Modified parameter definitions to support spec-only inputs:
```python
    async def execute(
        self,
        raw_data: Optional[str],
        spec: Optional[str] = None,
        report_path: Optional[str] = None,
        run_dry: bool = False,
    ) -> Path:
        # ...
        raw_data_str = str(Path(raw_data).resolve()) if raw_data else "Không cung cấp file database legacy (Chỉ phân tích nghiệp vụ spec)."
        instruction = Prompts.COMPATIBILITY_ANALYZER.format(
            raw_data_path=raw_data_str,
            spec_content=spec_content,
            schema_path=str(schema_file.resolve()),
            output_json_path=str(output_json.resolve())
        )
```

### File: `/home/ka/Repos/github.com/trongnghiango/kaos/src/kaos/application/use_cases/act_executor.py`
Updated `ActTask` data model:
```python
@dataclass
class ActTask:
    task_id: str
    title: str
    description: str
    complexity: TaskComplexity
    budget: TaskBudget
    module: str
    depends_on: List[str] = field(default_factory=list)
    status: str = "PENDING"
    result: dict = field(default_factory=dict)
```

### File: `/home/ka/Repos/github.com/trongnghiango/kaos/src/kaos/engine/task_queue_engine.py`
Fixed level verification logic:
```python
                levels_data = await self.knowledge_graph.calculate_levels()
                levels = levels_data.get("levels", {})
                if levels:
                    for lvl, tids in levels.items():
                        # ... mapping code ...
                    if self.level_groups:
                        # ... logging and return ...
```

### File: `/home/ka/Repos/github.com/trongnghiango/kaos/src/kaos/infrastructure/di.py`
Implemented `TYPE_CHECKING` imports and lazy resolution instances to eliminate circular dependencies. Added `/resolve` command handler for Git conflict resolution.

### File: `/home/ka/Repos/github.com/trongnghiango/kaos/run-kaos.sh`
Updated workspace paths and execution target environments:
```bash
export KAOS_TARGET_PATH="$REPO_ROOT/STAX_ASP"
export PYTHONPATH="$REPO_ROOT/kaos/src:$PYTHONPATH"
# ...
python3 "$SCRIPT_DIR/src/kaos/interfaces/cli.py" "$@"
```

### File: `/home/ka/Repos/github.com/trongnghiango/kaos/tests/use_cases/test_act_executor.py`
Injected mocked `GitPort` to execution environment:
```python
@pytest.fixture
def mock_git():
    m = AsyncMock()
    m.stash_push.return_value = None
    m.stash_pop.return_value = None
    m.checkout.return_value = True
    m.merge.return_value = (True, [])
    m.commit_all.return_value = True
    m.is_branch_exists.return_value = False
    m.push.return_value = True
    m.get_current_branch.return_value = "main"
    return m
```

---

## 4. Errors + Fixes
*   **Error**: `AttributeError: 'ActTask' object has no attribute 'result'` / `status`.
    *   *Fix*: Appended `status` and `result` fields to `ActTask` data structure definition.
*   **Error**: Test suite triggered actual git stashes and checkouts in the active workspace.
    *   *Fix*: Mocked `GitPort` interactions using async mocks inside `test_act_executor.py` fixtures.
*   **Error**: Circular import on use cases inside `di.py`.
    *   *Fix*: Moved use case annotations under `TYPE_CHECKING` and deferred imports to resolving function calls.
*   **Error**: Dry-run crashed if legacy database file `.xlsx` was missing.
    *   *Fix*: Updated argument validations and prompt builders to treat `raw_data` as optional when `spec` is provided.

---

## 5. Problem Solving
*   **Test Suite stability**: 105 tests are now successfully passing.
*   **STAX_ASP Dry-run Compatibility**: Verified spec-only analysis generated the expected `db_compatibility_report.md` output containing weighted design tables and clean Unified Diffs.
*   **Git workspace integrity**: Working tree on `/home/ka/Repos/github.com/trongnghiango/kaos` is clean. All active changes committed to the `develop` branch.

---

## 6. User Messages
*   *Complaint about kaos checking STAX files incorrectly.*
*   *Complaint about git changes and testing taking too long.*
*   *Request to save progress and commit changes to develop branch.*
*   *Request to verify Redis connectivity and task queue status.*
*   *Request to handoff the conversation details.*

---

## 7. Pending Tasks
*   **Auto-Refactoring Execution**: Resolving git conflicts inside `/home/ka/Repos/github.com/trongnghiango/STAX_ASP` to allow the active 55 refactoring tasks (splitting directories, relocating Drizzle schema packages) to run fully without stalling.
*   **RedisGraph Porting**: Complete native Cypher query compatibility integrations inside `RedisGraphAdapter` if migrating from simple Redis Hash/Set emulation structures to complex topological queries.

---

## 8. Current Work
*   Working directory is `/home/ka/Repos/github.com/trongnghiango/kaos` on the `develop` branch.
*   The workspace is fully verified, and clean. All updates are committed.

---

## 9. Suggested Skills
*   `git-guardian`: Must be used for any git operations.
*   `ka-be`: For backend clean architecture tasks and implementation details.
*   `ka-test`: For verifying test suites and coverage on new features.
*   `ka-workflow`: For overall workflow orchestration.
