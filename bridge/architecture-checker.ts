/**
 * Architecture Boundary Checker (AST-based)
 * =========================================
 * Chặng 2 của Hybrid Gatekeeper: Quét AST các file TypeScript để kiểm tra
 * vi phạm quy tắc kiến trúc theo file .kaos/architecture.yaml.
 *
 * Sử dụng TypeScript Compiler API (built-in, không cần cài thêm package).
 */

import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';
import * as YAML from 'yaml';

// ─── Interfaces ─────────────────────────────────────────────────────────────
export interface ArchitectureRule {
  layer: string;
  path_pattern: string;
  forbidden_imports: string[];
  allowed_imports?: string[];
  conventions?: Record<string, any>;
  description?: string;
}

export interface ArchitectureConfig {
  version: string;
  project_type: string;
  layers: ArchitectureRule[];
  conventions?: Record<string, any>;
}

export interface Violation {
  file: string;
  line: number;
  column: number;
  rule: string;
  message: string;
  severity: 'error' | 'warning';
  import_src?: string;
}

export interface CheckResult {
  success: boolean;
  violations: Violation[];
  summary: string;
}

// ─── Load configuration ─────────────────────────────────────────────────────
export function loadArchitectureConfig(targetPath: string): ArchitectureConfig {
  const configPath = path.join(targetPath, '.kaos', 'architecture.yaml');
  
  if (!fs.existsSync(configPath)) {
    return getDefaultConfig();
  }
  
  const content = fs.readFileSync(configPath, 'utf-8');
  return parseYamlConfig(content);
}

export function parseYamlConfig(yamlText: string): ArchitectureConfig {
  try {
    const parsed = YAML.parse(yamlText);
    
    const config: ArchitectureConfig = {
      version: String(parsed?.version || '1.0'),
      project_type: String(parsed?.project_type || 'nestjs-clean-architecture'),
      layers: [],
      conventions: {}
    };

    // Chuẩn hóa layers (hỗ trợ cả Array và Object)
    if (Array.isArray(parsed?.layers)) {
      for (const rawLayer of parsed.layers) {
        if (!rawLayer || typeof rawLayer !== 'object') continue;
        
        config.layers.push({
          layer: String(rawLayer.layer || ''),
          path_pattern: String(rawLayer.path_pattern || ''),
          forbidden_imports: Array.isArray(rawLayer.forbidden_imports) 
            ? rawLayer.forbidden_imports.map((item: any) => String(item)) 
            : [],
          allowed_imports: Array.isArray(rawLayer.allowed_imports)
            ? rawLayer.allowed_imports.map((item: any) => String(item))
            : [],
          description: rawLayer.description ? String(rawLayer.description) : undefined
        });
      }
    } else if (parsed?.layers && typeof parsed.layers === 'object') {
      // Hỗ trợ cấu trúc Object/Dictionary (như trong test suite)
      for (const [layerName, rawLayer] of Object.entries(parsed.layers)) {
        if (!rawLayer || typeof rawLayer !== 'object') continue;
        const typedLayer = rawLayer as any;
        
        config.layers.push({
          layer: layerName,
          path_pattern: String(typedLayer.path_pattern || ''),
          forbidden_imports: Array.isArray(typedLayer.forbidden_imports)
            ? typedLayer.forbidden_imports.map((item: any) => String(item))
            : [],
          allowed_imports: Array.isArray(typedLayer.allowed_imports)
            ? typedLayer.allowed_imports.map((item: any) => String(item))
            : [],
          description: typedLayer.description ? String(typedLayer.description) : undefined
        });
      }
    }

    // Chuẩn hóa conventions
    if (parsed?.conventions && typeof parsed.conventions === 'object') {
      for (const [key, value] of Object.entries(parsed.conventions)) {
        // Chuyển đổi chuỗi "true"/"false" hoặc boolean
        if (value === 'true' || value === true) {
          config.conventions![key] = true;
        } else if (value === 'false' || value === false) {
          config.conventions![key] = false;
        } else {
          config.conventions![key] = value;
        }
      }
    }

    return config;
  } catch (error: any) {
    console.error(`⚠️ Lỗi parse YAML: ${error.message}. Sử dụng default config.`);
    return getDefaultConfig();
  }
}

function getDefaultConfig(): ArchitectureConfig {
  return {
    version: '1.0',
    project_type: 'nestjs-clean-architecture',
    layers: [
      {
        layer: 'domain',
        path_pattern: '**/domain/**/*.ts',
        forbidden_imports: [
          '@nestjs/',
          '@drizzle-orm/',
          '**/infrastructure/**',
          '**/interfaces/**'
        ],
        description: 'Domain layer: Pure TypeScript entities and business logic'
      },
      {
        layer: 'application',
        path_pattern: '**/application/**/*.ts',
        forbidden_imports: [
          '**/infrastructure/**',
          '**/interfaces/**',
          '@nestjs/common',
          '@nestjs/core'
        ],
        description: 'Application layer: Use cases and ports'
      },
      {
        layer: 'infrastructure',
        path_pattern: '**/infrastructure/**/*.ts',
        forbidden_imports: [
          '**/interfaces/**'
        ],
        description: 'Infrastructure layer: Adapters and implementations'
      },
      {
        layer: 'interfaces',
        path_pattern: '**/interfaces/**/*.ts',
        forbidden_imports: [],
        description: 'Interfaces layer: Controllers and API endpoints'
      }
    ],
    conventions: {
      file_naming: 'kebab-case',
      strict_typescript: true,
      allow_any: true,
      role_casing: 'UPPERCASE'
    }
  };
}

// ─── AST Checker ────────────────────────────────────────────────────────────
export function checkArchitecture(
  filePaths: string[],
  config: ArchitectureConfig,
  repoRoot: string
): CheckResult {
  const violations: Violation[] = [];
  const targetPath = repoRoot;

  for (const filePath of filePaths) {
    const absolutePath = path.resolve(targetPath, filePath);
    if (!fs.existsSync(absolutePath)) continue;
    
    const sourceText = fs.readFileSync(absolutePath, 'utf-8');
    const sourceFile = ts.createSourceFile(
      filePath,
      sourceText,
      ts.ScriptTarget.Latest,
      true
    );
    
    const matchedLayer = findMatchingLayer(filePath, config.layers);
    if (!matchedLayer) continue;
    
    const violationsInFile = checkImportsInFile(
      sourceFile,
      filePath,
      matchedLayer,
      config
    );
    violations.push(...violationsInFile);
    
    if (config.conventions) {
      const conventionViolations = checkConventions(
        sourceFile,
        filePath,
        config.conventions,
        sourceText
      );
      violations.push(...conventionViolations);
    }
  }
  
  return {
    success: violations.length === 0,
    violations,
    summary: violations.length > 0
      ? `❌ Found ${violations.length} architecture violation(s)`
      : '✅ All architecture rules passed'
  };
}

function findMatchingLayer(
  filePath: string,
  layers: ArchitectureRule[]
): ArchitectureRule | null {
  for (const layer of layers) {
    let pattern = layer.path_pattern
      .replace(/\./g, '\\.')              // . -> \.
      .replace(/\*\*/g, '____DOUBLE_STAR____')
      .replace(/\*/g, '[^/]*')            // * -> [^/]*
      .replace(/____DOUBLE_STAR____/g, '.*'); // ** -> .*
    
    if (!pattern.startsWith('^')) {
      pattern = '^' + pattern;
    }
    if (!pattern.endsWith('$')) {
      pattern = pattern + '$';
    }
    
    try {
      const regex = new RegExp(pattern);
      if (regex.test(filePath)) {
        return layer;
      }
    } catch (e) {
      // Ignore invalid regex
    }
  }
  return null;
}

function checkImportsInFile(
  sourceFile: ts.SourceFile,
  filePath: string,
  layer: ArchitectureRule,
  config: ArchitectureConfig
): Violation[] {
  const violations: Violation[] = [];
  
  function walkNode(node: ts.Node) {
    if (ts.isImportDeclaration(node)) {
      const importPath = (node.moduleSpecifier as ts.StringLiteral).text;
      const line = sourceFile.getLineAndCharacterOfPosition(node.getStart());
      
      for (const forbidden of layer.forbidden_imports) {
        if (matchImportPattern(importPath, forbidden)) {
          violations.push({
            file: filePath,
            line: line.line + 1,
            column: line.character + 1,
            rule: `${layer.layer}-purity`,
            message: `Import '${importPath}' is forbidden in ${layer.layer} layer (matches forbidden pattern: '${forbidden}'). ${layer.description || ''}`,
            severity: 'error',
            import_src: importPath
          });
        }
      }
    }
    
    ts.forEachChild(node, walkNode);
  }
  
  walkNode(sourceFile);
  return violations;
}

function matchImportPattern(importPath: string, pattern: string): boolean {
  let regexPattern = pattern
    .replace(/\./g, '\\.')
    .replace(/\*\*/g, '____DOUBLE_STAR____')
    .replace(/\*/g, '[^/]*')
    .replace(/____DOUBLE_STAR____/g, '.*');
  
  if (!regexPattern.startsWith('^')) {
    regexPattern = '^' + regexPattern;
  }
  if (!regexPattern.endsWith('$')) {
    regexPattern = regexPattern + '$';
  }
  
  try {
    const regex = new RegExp(regexPattern);
    return regex.test(importPath);
  } catch {
    return importPath.includes(pattern.replace(/\*/g, ''));
  }
}

function checkConventions(
  sourceFile: ts.SourceFile,
  filePath: string,
  conventions: Record<string, any>,
  sourceText: string
): Violation[] {
  const violations: Violation[] = [];
  
  if (conventions.allow_any === false || conventions.allow_any === 'false') {
    function walkForAny(node: ts.Node) {
      if (node.kind === ts.SyntaxKind.AnyKeyword) {
        const line = sourceFile.getLineAndCharacterOfPosition(node.getStart());
        violations.push({
          file: filePath,
          line: line.line + 1,
          column: line.character + 1,
          rule: 'no-explicit-any',
          message: `Usage of 'any' type is forbidden. Use proper type definitions instead.`,
          severity: 'error'
        });
      }
      ts.forEachChild(node, walkForAny);
    }
    walkForAny(sourceFile);
  }
  
  if (conventions.role_casing === 'UPPERCASE') {
    const rolePattern = /['"](admin|super_admin|manager|user|viewer)['"]/gi;
    let match;
    while ((match = rolePattern.exec(sourceText)) !== null) {
      const line = sourceFile.getLineAndCharacterOfPosition(match.index);
      if (match[1] !== match[1].toUpperCase()) {
        violations.push({
          file: filePath,
          line: line.line + 1,
          column: match.index - sourceText.lastIndexOf('\n', match.index),
          rule: 'role-casing',
          message: `Role '${match[1]}' should be UPPERCASE ('${match[1].toUpperCase()}') to match RBAC convention.`,
          severity: 'warning'
        });
      }
    }
  }
  
  return violations;
}

// ─── CLI Entry Point ────────────────────────────────────────────────────────
if (require.main === module) {
  const args = process.argv.slice(2);
  
  if (args.length < 2) {
    console.log(JSON.stringify({
      success: false,
      violations: [],
      summary: 'Usage: tsx architecture-checker.ts <target_path> <file1> [file2 ...]'
    }));
    process.exit(0);
  }
  
  const targetPath = path.resolve(args[0]);
  const filesToCheck = args.slice(1).map(f => f.startsWith('/') ? f : path.resolve(process.cwd(), f));
  const relativeFiles = filesToCheck.map(f => path.relative(targetPath, f));
  
  const config = loadArchitectureConfig(targetPath);
  const result = checkArchitecture(relativeFiles, config, targetPath);
  
  console.log(JSON.stringify(result, null, 2));
  process.exit(result.success ? 0 : 1);
}