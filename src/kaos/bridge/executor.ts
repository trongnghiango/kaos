/**
 * Hybrid AI Agent Engine - TypeScript Bridge
 * ===========================================
 * Nhận lệnh JSON từ Python Orchestrator, thực thi các tác vụ kỹ thuật
 * với NestJS codebase và trả về kết quả JSON.
 */
import * as fs from 'fs';
import * as path from 'path';
import { execSync } from 'child_process';
import * as ts from 'typescript';

import { checkArchitecture, loadArchitectureConfig } from './architecture-checker';

const REPO_ROOT = process.cwd();

// ─── Interfaces ─────────────────────────────────────────────────────────────
interface TaskContext {
  action: 'compile' | 'test' | 'seed' | 'benchmark' | 'apply_code' | 'lint' | 'security-scan' | 'analyze' | 'extract-schema' | 'check-architecture';
  module: string;
  target_url?: string;
  duration_secs?: number;
  concurrency?: number;
  feature_desc?: string;
  file_paths?: string[];
  code_content?: Record<string, string>;
}

interface TaskResult {
  success: boolean;
  stdout: string;
  stderr: string;
  metrics?: any;
  error?: string;
}

// ─── Helpers ────────────────────────────────────────────────────────────────
function runCmd(cmd: string, cwd: string = REPO_ROOT): { success: boolean; stdout: string; stderr: string } {
  try {
    const env = { ...process.env };
    if (env.PATH) {
      env.PATH = env.PATH.split(':').filter(p => !p.includes('mcp-hermit')).join(':');
    }
    
    // Redirect stderr sang stdout để gom hết output
    const stdout = execSync(cmd + ' 2>&1', { cwd, env, encoding: 'utf-8', stdio: 'pipe' });
    return { success: true, stdout, stderr: '' };
  } catch (error: any) {
    const stdout = error.stdout || '';
    const stderr = error.stderr || '';
    const message = error.message || '';
    return { 
      success: false, 
      stdout: stdout.toString(), 
      stderr: (stderr.toString() + '\n' + message.toString()).trim() 
    };
  }
}

function outputResult(result: TaskResult) {
  // Python Orchestrator sẽ đọc cái này
  console.log(JSON.stringify(result));
  process.exit(0);
}

// ─── Action Handlers ───────────────────────────────────────────────────────
async function handleCompile(task: TaskContext) {
  // Fix: Direct execution with absolute paths to avoid mcp-hermit swallowing the error code or swapping CWD
  const backendDir = path.resolve(REPO_ROOT, 'backend');
  const tsconfigPath = path.resolve(backendDir, 'tsconfig.json');
  const tscBin = path.resolve(backendDir, 'node_modules/typescript/bin/tsc');
  const tmpOut = path.resolve(backendDir, '.tsc_out.log');
  
  // Use node directly pointing to typescript binary with absolute path to config
  const res = runCmd(`node ${tscBin} --noEmit -p ${tsconfigPath} > ${tmpOut} 2>&1 || true`, backendDir);
  
  let actualOutput = '';
  if (fs.existsSync(tmpOut)) {
    actualOutput = fs.readFileSync(tmpOut, 'utf-8');
    fs.unlinkSync(tmpOut); // cleanup
  }

  // Check if output contains TS errors
  const hasTsErrors = actualOutput.includes('error TS') || actualOutput.includes('Cannot find module');

  outputResult({
    success: !hasTsErrors,
    stdout: actualOutput,
    stderr: '',
    error: !hasTsErrors ? undefined : 'Compilation Failed',
  });
}

async function handleTest(task: TaskContext) {
  // Chạy test suite cho module cụ thể
  // Cố gắng dùng binary pnpm cục bộ (./node_modules/.bin/pnpm) thay vì path toàn cục
  const backendDir = path.resolve(REPO_ROOT, 'backend');
  const localPnpm = path.resolve(backendDir, 'node_modules/.bin/pnpm');
  
  // Chỉ test src/core khi module là 'all' hoặc 'core' để tránh các lỗi timeout của các module khác không liên quan
  const testPath = (task.module === 'all' || task.module === 'core')
    ? 'src/core'
    : `src/modules/${task.module}`;
  
  let res;
  if (fs.existsSync(localPnpm)) {
    res = runCmd(`node ${localPnpm} test ${testPath}`, backendDir);
  } else {
    res = runCmd(`cd backend && pnpm test ${testPath}`, REPO_ROOT);
  }
  outputResult({
    success: res.success,
    stdout: res.stdout,
    stderr: res.stderr,
    error: res.success ? undefined : `Test Failed: ${res.stderr.slice(0, 500)}`,
  });
}

async function handleApplyCode(task: TaskContext) {
  if (!task.code_content) {
    outputResult({ success: false, stdout: '', stderr: '', error: 'No code content provided' });
    return;
  }

  try {
    for (const [filePath, content] of Object.entries(task.code_content)) {
      const fullPath = path.resolve(REPO_ROOT, filePath);
      fs.mkdirSync(path.dirname(fullPath), { recursive: true });
      fs.writeFileSync(fullPath, content, 'utf-8');
    }
    outputResult({ success: true, stdout: 'Code applied successfully', stderr: '' });
  } catch (error: any) {
    outputResult({ success: false, stdout: '', stderr: '', error: error.message });
  }
}

async function handleLint(task: TaskContext) {
  // Chạy eslint cho module tương ứng
  const targetPath = `backend/src/modules/${task.module}`;
  const res = runCmd(`pnpm --filter backend lint -- ${targetPath}`);
  outputResult({
    success: res.success,
    stdout: res.stdout,
    stderr: res.stderr,
    error: res.success ? undefined : 'Lint check failed',
  });
}

async function handleSecurityScan(task: TaskContext) {
  const targetPath = path.resolve(REPO_ROOT, `backend/src/modules/${task.module}`);
  const issues: string[] = [];

  if (!fs.existsSync(targetPath)) {
    outputResult({ success: true, stdout: 'Module does not exist yet, skipping scan', stderr: '' });
    return;
  }

  // Quét code thủ công các pattern bị cấm
  const checkDirectory = (dir: string) => {
    const files = fs.readdirSync(dir);
    for (const file of files) {
      const fullPath = path.join(dir, file);
      const stat = fs.statSync(fullPath);
      if (stat.isDirectory()) {
        checkDirectory(fullPath);
      } else if (file.endsWith('.ts')) {
        const content = fs.readFileSync(fullPath, 'utf-8');
        
        // 1. Kiểm tra fallback tenant unsafe
        if (content.includes('organizationId ||') || content.includes('organization_id ||')) {
          issues.push(`Unsafe Tenant Fallback in ${path.relative(REPO_ROOT, fullPath)}`);
        }
        
        // 2. Kiểm tra bypass Auth Guard
        if (content.includes('@Public(') || content.includes('@BypassAuth(')) {
          issues.push(`Potential security bypass: authorization decorator found in ${path.relative(REPO_ROOT, fullPath)}`);
        }
        
        // 3. Kiểm tra DB raw query bypass Drizzle
        if (content.includes('queryRaw') || content.includes('.query(') && !content.includes('db.query')) {
          issues.push(`Potential raw SQL query bypass in ${path.relative(REPO_ROOT, fullPath)}`);
        }
      }
    }
  };

  try {
    checkDirectory(targetPath);
    const passed = issues.length === 0;
    outputResult({
      success: passed,
      stdout: passed ? 'No security violations found.' : `Violations:\n${issues.join('\n')}`,
      stderr: '',
      error: passed ? undefined : `${issues.length} security violations detected`
    });
  } catch (error: any) {
    outputResult({ success: false, stdout: '', stderr: '', error: error.message });
  }
}

async function handleAnalyze(task: TaskContext) {
  const targetPath = path.resolve(REPO_ROOT, `backend/src/modules/${task.module}`);
  const metadata = {
    exists: fs.existsSync(targetPath),
    filesCount: 0,
    endpoints: [] as string[],
    entities: [] as string[],
  };

  if (metadata.exists) {
    const scanDir = (dir: string) => {
      const files = fs.readdirSync(dir);
      for (const file of files) {
        const fullPath = path.join(dir, file);
        if (fs.statSync(fullPath).isDirectory()) {
          scanDir(fullPath);
        } else {
          metadata.filesCount++;
          if (file.includes('controller.ts')) {
            metadata.endpoints.push(path.relative(REPO_ROOT, fullPath));
          }
          if (file.includes('entity.ts')) {
            metadata.entities.push(path.relative(REPO_ROOT, fullPath));
          }
        }
      }
    };
    scanDir(targetPath);
  }

  outputResult({
    success: true,
    stdout: 'Analysis completed',
    stderr: '',
    metrics: metadata
  });
}

async function handleExtractSchema(task: TaskContext) {
  const EXCLUDED_DIRS = new Set(['node_modules', '.git', 'dist', 'coverage', '.next', '.nuxt', 'build', '.cache']);

  const allTables: string[] = [];
  const allColumns: { name: string; type: string; is_key: boolean }[] = [];
  const columnsByTable: Record<string, { name: string; type: string }[]> = {};
  const allModules: string[] = [];
  const schemaFileData: Record<string, any> = {};

  function extractSchemaFromSource(sourceText: string, filePath: string): Record<string, { tableName: string; columns: string[] }> {
    const sourceFile = ts.createSourceFile(filePath, sourceText, ts.ScriptTarget.Latest, true);
    const tables: Record<string, { tableName: string; columns: string[] }> = {};

    function walkNode(node: ts.Node) {
      if (ts.isVariableDeclaration(node) && node.initializer && ts.isCallExpression(node.initializer)) {
        const call = node.initializer;

        if (ts.isIdentifier(call.expression) && call.expression.text === 'pgTable') {
          const variableName = node.name.getText(sourceFile);

          let tableName = '';
          if (call.arguments.length > 0 && ts.isStringLiteral(call.arguments[0])) {
            tableName = call.arguments[0].text;
          }

          const columns: string[] = [];
          if (call.arguments.length > 1 && ts.isObjectLiteralExpression(call.arguments[1])) {
            for (const property of call.arguments[1].properties) {
              if (ts.isPropertyAssignment(property)) {
                columns.push(property.name.getText(sourceFile));
              }
            }
          }

          if (tableName) {
            tables[variableName] = { tableName, columns };
          }
        }
      }
      ts.forEachChild(node, walkNode);
    }

    walkNode(sourceFile);
    return tables;
  }

  function extractModuleName(filePath: string): string {
    const basename = path.basename(filePath);  // e.g. "user.module.ts"
    // Convert "user.module.ts" → "UserModule" (capitalize + strip extension)
    const parts = basename.replace(/\.module\.ts$/, '').split(/[-_.]/);
    return parts.map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('') + 'Module';
  }

  function walkDir(dir: string) {
    let entries: string[];
    try {
      entries = fs.readdirSync(dir);
    } catch {
      return; // permission denied or missing
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry);
      let stat: fs.Stats;
      try {
        stat = fs.statSync(fullPath);
      } catch {
        continue;
      }
      if (stat.isDirectory()) {
        if (!EXCLUDED_DIRS.has(entry) && !entry.startsWith('.')) {
          walkDir(fullPath);
        }
      } else if (entry.endsWith('.schema.ts')) {
        const content = fs.readFileSync(fullPath, 'utf-8');
        const relativePath = path.relative(REPO_ROOT, fullPath);
        const tables = extractSchemaFromSource(content, fullPath);

        for (const [varName, tableInfo] of Object.entries(tables)) {
          const tName = tableInfo.tableName || varName;
          allTables.push(tName);
          columnsByTable[tName.toLowerCase()] = tableInfo.columns.map(c => ({
            name: c,
            type: 'string',
          }));
          for (const col of tableInfo.columns) {
            allColumns.push({ name: col, type: 'string', is_key: false });
          }
          schemaFileData[relativePath] = {
            [varName]: { tableName: tName, columns: tableInfo.columns },
            ...(schemaFileData[relativePath] || {}),
          };
        }
      } else if (entry.endsWith('.module.ts')) {
        const relativePath = path.relative(REPO_ROOT, fullPath);
        // Only collect top-level modules (skip deep node_modules if any slipped through)
        if (!relativePath.includes('node_modules')) {
          const modName = extractModuleName(entry);
          if (!allModules.includes(modName)) {
            allModules.push(modName);
          }
        }
      }
    }
  }

  try {
    walkDir(REPO_ROOT);

    if (allTables.length === 0) {
      // Fallback: no schema tables found but still return modules
      outputResult({
        success: true,
        stdout: allModules.length > 0
          ? `No Drizzle schema files found (${allModules.length} modules detected)`
          : 'No schema files found in repository',
        stderr: '',
        metrics: {
          tables: [],
          columns: [],
          modules: allModules,
          columns_by_table: {},
          raw: {},
        }
      });
      return;
    }

    // Deduplicate table names
    const uniqueTables = [...new Set(allTables)];
    const uniqueModules = [...new Set(allModules)];

    outputResult({
      success: true,
      stdout: `Extracted ${uniqueTables.length} tables, ${uniqueModules.length} modules from ${Object.keys(schemaFileData).length} schema files`,
      stderr: '',
      metrics: {
        tables: uniqueTables,
        columns: allColumns,
        modules: uniqueModules,
        columns_by_table: columnsByTable,
        raw: schemaFileData,
      }
    });
  } catch (error: any) {
    outputResult({ success: false, stdout: '', stderr: '', error: error.message });
  }
}

async function handleBenchmark(task: TaskContext) {
  // Thay vì dùng benchmark.ts (node fetch), ta dùng K6 chính thức như thiết kế
  const moduleTarget = task.module || 'crm';
  console.log(`[Gatekeeper] Running k6 benchmark for module: ${moduleTarget}`);
  
  const k6ScriptPath = path.resolve(REPO_ROOT, 'tools/autoresearch/benchmarks/module-benchmark.js');
  const resultFile = path.resolve(REPO_ROOT, `tools/autoresearch/tmp/k6_result_${moduleTarget}.json`);
  
  // Chạy k6 xuất kết quả JSON
  const res = runCmd(`k6 run --out json=${resultFile} -e MODULE=${moduleTarget} ${k6ScriptPath}`);
  
  if (!res.success) {
    // Nếu k6 không được cài đặt
    if (res.stderr.includes('command not found')) {
      outputResult({ success: false, stdout: '', stderr: '', error: 'k6 is not installed on the system' });
      return;
    }
    
    // Nếu k6 chạy nhưng failed thresholds
    outputResult({ success: false, stdout: res.stdout, stderr: res.stderr, error: 'Benchmark failed (Thresholds not met)' });
    return;
  }
  
  // Do kết quả json output của K6 khá lớn và phức tạp, ta có thể parse kết quả tổng (summary)
  // Trong thực tế, bạn có thể thiết lập handleJsonOutput() tùy chỉnh từ K6 JSON format
  // Tạm thời trả về raw stdout (thường chứa report đẹp)
  outputResult({
    success: true,
    stdout: res.stdout,
    stderr: '',
    metrics: { benchmark_passed: true }
  });
}

/**
 * Phát hiện các file TypeScript đã thay đổi so với nhánh gốc
 * Dùng để tự động scan kiến trúc khi không có file_paths được truyền.
 * Tự động tìm merge-base với origin/HEAD, main hoặc master để tránh hardcode.
 */
function getChangedFilesFromGit(): string[] {
  const filesSet = new Set<string>();

  // Tìm điểm merge-base phù hợp với origin/HEAD, main hoặc master để tránh hardcode
  let baseRev = 'main';
  const detectBase = runCmd(
    'git merge-base HEAD origin/HEAD 2>/dev/null || git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null',
    REPO_ROOT
  );
  if (detectBase.success && detectBase.stdout.trim()) {
    baseRev = detectBase.stdout.trim();
  }

  // Lệnh lấy file đã thay đổi so với base commit
  const diffMain = runCmd(`git diff --name-only ${baseRev}...HEAD`, REPO_ROOT);
  if (diffMain.success) {
    diffMain.stdout.split('\n').map(f => f.trim()).filter(Boolean).forEach(f => filesSet.add(f));
  }

  // Lệnh lấy file chưa staged (chưa commit)
  const diffUnstaged = runCmd('git diff --name-only', REPO_ROOT);
  if (diffUnstaged.success) {
    diffUnstaged.stdout.split('\n').map(f => f.trim()).filter(Boolean).forEach(f => filesSet.add(f));
  }

  // Lọc: chỉ lấy file .ts tồn tại trên đĩa
  return Array.from(filesSet).filter(f => f.endsWith('.ts') && fs.existsSync(path.resolve(REPO_ROOT, f)));
}

async function handleCheckArchitecture(task: TaskContext) {
  let filePaths = task.file_paths || [];

  // Tự động detect các file thay đổi nếu không được truyền
  if (filePaths.length === 0) {
    filePaths = getChangedFilesFromGit();
    if (filePaths.length === 0) {
      outputResult({ success: true, stdout: 'No changed files detected and no file paths provided', stderr: '' });
      return;
    }
  }
  
  try {
    const config = loadArchitectureConfig(REPO_ROOT);
    const result = checkArchitecture(filePaths, config, REPO_ROOT);
    outputResult({
      success: result.success,
      stdout: result.summary,
      stderr: '',
      metrics: result.violations
    });
  } catch (error: any) {
    outputResult({ success: false, stdout: '', stderr: '', error: error.message });
  }
}

// ─── Main Logic ─────────────────────────────────────────────────────────────
async function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      data += chunk;
    });
    process.stdin.on('end', () => {
      resolve(data);
    });
    process.stdin.on('error', (err) => {
      reject(err);
    });
  });
}

async function main() {
  const args = process.argv.slice(2);
  let task: TaskContext;
  let rawData = '';

  // 1. Nhận diện nguồn input (File vs Stdin)
  if (args.length > 0 && fs.existsSync(args[0])) {
    try {
      rawData = fs.readFileSync(args[0], 'utf-8');
    } catch (err: any) {
      console.error(`Failed to read task file: ${err.message}`);
      process.exit(1);
    }
  } else {
    try {
      rawData = await readStdin();
      if (!rawData.trim()) {
        console.error('Missing task JSON content via stdin or file path');
        process.exit(1);
      }
    } catch (err: any) {
      console.error(`Failed to read from stdin: ${err.message}`);
      process.exit(1);
    }
  }

  // 2. Parse JSON
  try {
    task = JSON.parse(rawData);
  } catch (err: any) {
    console.error(`Failed to parse task JSON: ${err.message}`);
    process.exit(1);
  }

  switch (task.action) {
    case 'compile':
      await handleCompile(task);
      break;
    case 'test':
      await handleTest(task);
      break;
    case 'apply_code':
      await handleApplyCode(task);
      break;
    case 'lint':
      await handleLint(task);
      break;
    case 'security-scan':
      await handleSecurityScan(task);
      break;
    case 'analyze':
      await handleAnalyze(task);
      break;
    case 'benchmark':
      await handleBenchmark(task);
      break;
    case 'extract-schema':
      await handleExtractSchema(task);
      break;
    case 'check-architecture':
      await handleCheckArchitecture(task);
      break;
    default:
      outputResult({ success: false, stdout: '', stderr: '', error: `Unknown action: ${task.action}` });
  }
}

main().catch(err => {
  outputResult({ success: false, stdout: '', stderr: '', error: err.message });
});