/**
 * KAOS Codebase Scanner — TypeScript Compiler API AST Parser
 * ===========================================================
 * Dùng TypeScript Compiler API để parse cấu trúc codebase.
 * 
 * Output (stdout): JSON array of CodeFunctionNode objects.
 * 
 * Usage:
 *   tsx src/kaos/bridge/codebase-scanner.ts \
 *     --path /path/to/STAX_ASP \
 *     [--files src/file1.ts,src/file2.ts] \
 *     [--format json]
 * 
 * Input:
 *   --path      Absolute path to target codebase (required)
 *   --files     Comma-separated specific files to scan (optional)
 *   --exclude   Comma-separated glob patterns to exclude (optional)
 * 
 * Output:
 *   JSON array stdout — mỗi element là 1 function/method node
 */

import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';
import * as ts from 'typescript';

// ─── Interfaces ─────────────────────────────────────────────────────────────

interface ImportInfo {
  module: string;
  imported_names: string[];
}

interface FunctionNode {
  function_name: string;
  file_path: string;
  start_line: number;
  end_line: number;
  is_exported: boolean;
  is_async: boolean;
  node_type: string;
  class_name: string | null;
  access_modifier: string;
  imports: ImportInfo[];
  callee_functions: string[];
  file_hash: string;
}

// ─── CLI Args ───────────────────────────────────────────────────────────────

function parseArgs(): { targetPath: string; files?: string[]; exclude?: string[] } {
  const args = process.argv.slice(2);
  const parsed: Record<string, string> = {};

  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith('--')) {
      const key = args[i].slice(2);
      const val = args[i + 1] && !args[i + 1].startsWith('--') ? args[i + 1] : 'true';
      parsed[key] = val;
      if (val !== 'true') i++;
    }
  }

  const files = parsed['files'] ? parsed['files'].split(',').map(f => f.trim()).filter(Boolean) : undefined;
  const exclude = parsed['exclude'] ? parsed['exclude'].split(',').map(f => f.trim()).filter(Boolean) : undefined;

  if (!parsed['path']) {
    console.error(JSON.stringify({ error: 'Missing required argument: --path' }));
    process.exit(1);
  }

  return { targetPath: parsed['path'], files, exclude };
}

// ─── Helpers ────────────────────────────────────────────────────────────────

const EXCLUDED_DIRS = new Set([
  'node_modules', '.git', 'dist', 'coverage', '.next', '.nuxt',
  'build', '.cache', 'tmp', 'temp', '.venv', 'venv', '__pycache__',
]);

function computeFileHash(filePath: string): string {
  try {
    const content = fs.readFileSync(filePath, 'utf-8');
    return crypto.createHash('md5').update(content).digest('hex');
  } catch {
    return '';
  }
}

function getAllTsFiles(dir: string, exclude?: string[]): string[] {
  const excludeSet = new Set(exclude || []);
  const results: string[] = [];

  function walk(currentDir: string) {
    let entries: string[];
    try {
      entries = fs.readdirSync(currentDir);
    } catch {
      return;
    }

    for (const entry of entries) {
      const fullPath = path.join(currentDir, entry);
      const relativePath = path.relative(dir, fullPath);

      // Skip excluded dirs
      if (EXCLUDED_DIRS.has(entry) || entry.startsWith('.')) continue;

      // Skip user-defined exclude patterns
      if (excludeSet.has(entry) || excludeSet.has(relativePath)) continue;

      let stat: fs.Stats;
      try {
        stat = fs.statSync(fullPath);
      } catch {
        continue;
      }

      if (stat.isDirectory()) {
        walk(fullPath);
      } else if (
        entry.endsWith('.ts') &&
        !entry.endsWith('.d.ts') &&
        !entry.endsWith('.spec.ts') &&
        !entry.endsWith('.test.ts')
      ) {
        results.push(relativePath);
      }
    }
  }

  walk(dir);
  return results;
}

// ─── AST Parsing Functions ──────────────────────────────────────────────────

function getAccessModifier(modifiers: ts.ModifierLike[] | undefined): string {
  if (!modifiers) return 'public';
  for (const m of modifiers) {
    const text = m.getText().toLowerCase();
    if (text === 'public' || text === 'private' || text === 'protected') return text;
  }
  return 'public';
}

function isExported(modifiers: ts.ModifierLike[] | undefined): boolean {
  if (!modifiers) return false;
  return modifiers.some(m => m.kind === ts.SyntaxKind.ExportKeyword);
}

function isAsync(modifiers: ts.ModifierLike[] | undefined): boolean {
  if (!modifiers) return false;
  return modifiers.some(m => m.kind === ts.SyntaxKind.AsyncKeyword);
}

function extractImportInfo(node: ts.ImportDeclaration): ImportInfo | null {
  const moduleText = (node.moduleSpecifier as ts.StringLiteral).text;
  const importedNames: string[] = [];

  const importClause = node.importClause;
  if (!importClause) return null;

  // Named imports: import { A, B } from 'module'
  if (importClause.namedBindings && ts.isNamedImports(importClause.namedBindings)) {
    for (const element of importClause.namedBindings.elements) {
      importedNames.push(element.name.text);
    }
  }

  // Default import: import X from 'module'
  if (importClause.name) {
    importedNames.push(importClause.name.text);
  }

  return importedNames.length > 0 ? { module: moduleText, imported_names: importedNames } : null;
}

function extractCalleeNames(node: ts.Node, sourceFile: ts.SourceFile): string[] {
  const callees: string[] = [];

  function walk(n: ts.Node) {
    if (ts.isCallExpression(n)) {
      const expr = n.expression;
      if (ts.isPropertyAccessExpression(expr)) {
        // obj.method() → "obj.method"
        callees.push(expr.getText(sourceFile));
      } else if (ts.isIdentifier(expr)) {
        // function() → "function"
        callees.push(expr.text);
      }
    }
    ts.forEachChild(n, walk);
  }

  walk(node);
  return callees;
}

function extractImports(sourceFile: ts.SourceFile): ImportInfo[] {
  const imports: ImportInfo[] = [];

  function walk(node: ts.Node) {
    if (ts.isImportDeclaration(node)) {
      const info = extractImportInfo(node);
      if (info) imports.push(info);
    }
    ts.forEachChild(node, walk);
  }

  walk(sourceFile);
  return imports;
}

function extractFunctionName(node: ts.FunctionLikeDeclaration, sourceFile: ts.SourceFile): string {
  if (node.name) {
    return node.name.getText(sourceFile);
  }
  // Arrow function or anonymous — use surrounding context
  return `anonymous_${node.getStart(sourceFile)}`;
}

function getNodeType(node: ts.Node): string {
  if (ts.isMethodDeclaration(node)) return 'method';
  if (ts.isFunctionDeclaration(node)) return 'function';
  if (ts.isArrowFunction(node)) return 'arrow_function';
  if (ts.isFunctionExpression(node)) return 'function';
  if (ts.isConstructorDeclaration(node)) return 'constructor';
  return 'function';
}

function getClassName(node: ts.Node): string | null {
  let parent = node.parent;
  while (parent) {
    if (ts.isClassDeclaration(parent) && parent.name) {
      return parent.name.text;
    }
    parent = parent.parent;
  }
  return null;
}

function parseFile(filePath: string, relativePath: string, sourceFile?: ts.SourceFile): FunctionNode[] {
  const absolutePath = path.resolve(filePath);
  const content = fs.readFileSync(absolutePath, 'utf-8');
  const sf = sourceFile || ts.createSourceFile(relativePath, content, ts.ScriptTarget.Latest, true);
  const fileHash = computeFileHash(absolutePath);
  const fileImports = extractImports(sf);
  const nodes: FunctionNode[] = [];

  const { lineMap } = (sf as any);
  function getLine(pos: number): number {
    if (lineMap) {
      // Binary search for line number
      let lo = 0, hi = lineMap.length - 1;
      while (lo < hi) {
        const mid = Math.floor((lo + hi + 1) / 2);
        if (lineMap[mid] <= pos) lo = mid;
        else hi = mid - 1;
      }
      return lo + 1; // 1-indexed
    }
    // Fallback: count newlines
    return content.substring(0, pos).split('\n').length;
  }

  function walkNode(node: ts.Node) {
    if (
      ts.isFunctionDeclaration(node) ||
      ts.isMethodDeclaration(node) ||
      ts.isFunctionExpression(node) ||
      ts.isArrowFunction(node) ||
      ts.isConstructorDeclaration(node)
    ) {
      // Skip nested arrow functions (they're part of the parent body)
      // Only capture top-level functions and class methods at first level
      const parent = node.parent;
      if (
        ts.isArrowFunction(parent) ||
        ts.isFunctionExpression(parent)
      ) {
        // Nested, skip — we captured the parent already
        ts.forEachChild(node, walkNode);
        return;
      }

      const modifiers = (node as any).modifiers as ts.ModifierLike[] | undefined;
      const funcName = extractFunctionName(node, sf);
      const callees = extractCalleeNames(node, sf);
      const startLine = getLine(node.getStart(sf));
      const endLine = getLine(node.getEnd());

      // Skip anonymous lambdas that are not class properties
      if (funcName.startsWith('anonymous_') && !getClassName(node)) {
        ts.forEachChild(node, walkNode);
        return;
      }

      const funcNode: FunctionNode = {
        function_name: funcName,
        file_path: relativePath,
        start_line: startLine,
        end_line: endLine,
        is_exported: isExported(modifiers),
        is_async: isAsync(modifiers),
        node_type: getNodeType(node),
        class_name: getClassName(node),
        access_modifier: getAccessModifier(modifiers),
        imports: fileImports,
        callee_functions: callees.filter((c, i, a) => a.indexOf(c) === i), // unique
        file_hash: fileHash,
      };

      nodes.push(funcNode);
    }

    ts.forEachChild(node, walkNode);
  }

  walkNode(sf);
  return nodes;
}

// ─── Main ───────────────────────────────────────────────────────────────────

function main() {
  const { targetPath, files, exclude } = parseArgs();
  const resolvedPath = path.resolve(targetPath);

  if (!fs.existsSync(resolvedPath)) {
    console.error(JSON.stringify({ error: `Target path does not exist: ${resolvedPath}` }));
    process.exit(1);
  }

  // Get tsconfig if available
  const tsconfigPath = path.resolve(resolvedPath, 'tsconfig.json');
  let program: ts.Program | undefined;
  let typeChecker: ts.TypeChecker | undefined;

  if (fs.existsSync(tsconfigPath)) {
    const configFile = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
    if (configFile.config) {
      const parsedConfig = ts.parseJsonConfigFileContent(
        configFile.config,
        ts.sys,
        resolvedPath
      );
      program = ts.createProgram({
        rootNames: parsedConfig.fileNames,
        options: parsedConfig.options,
      });
      typeChecker = program.getTypeChecker();
    }
  }

  // Collect files to scan
  const filesToScan = files || getAllTsFiles(resolvedPath, exclude);

  // Parse each file
  const allNodes: FunctionNode[] = [];

  for (const relPath of filesToScan) {
    const absPath = path.resolve(resolvedPath, relPath);
    if (!fs.existsSync(absPath)) continue;

    try {
      const nodes = parseFile(absPath, relPath);
      allNodes.push(...nodes);
    } catch (err: any) {
      // Skip files that fail to parse
      console.error(JSON.stringify({ warning: `Failed to parse ${relPath}: ${err.message}` }), process.stderr);
    }
  }

  // Output JSON
  console.log(JSON.stringify(allNodes));
}

main();
