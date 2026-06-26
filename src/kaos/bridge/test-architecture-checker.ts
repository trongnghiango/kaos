import * as fs from 'fs';
import * as path from 'path';
import { checkArchitecture, loadArchitectureConfig } from './architecture-checker';

// Tạo môi trường giả lập tạm thời để kiểm thử
const tempDir = path.resolve(__dirname, 'temp_test_project');
const kaosDir = path.join(tempDir, '.kaos');

if (!fs.existsSync(tempDir)) {
  fs.mkdirSync(tempDir, { recursive: true });
}
if (!fs.existsSync(kaosDir)) {
  fs.mkdirSync(kaosDir, { recursive: true });
}

// 1. Ghi file architecture.yaml cấu hình kiểm thử
const configYaml = `
version: "1.0"
project_type: "nestjs-clean-architecture"
layers:
  domain:
    path_pattern: "**/domain/**/*.ts"
    forbidden_imports:
      - "@nestjs/**"
      - "**/infrastructure/**"
  infrastructure:
    path_pattern: "**/infrastructure/**/*.ts"
    forbidden_imports:
      - "**/interfaces/**"
conventions:
  allow_any: false
  role_casing: "UPPERCASE"
`;
fs.writeFileSync(path.join(kaosDir, 'architecture.yaml'), configYaml, 'utf-8');

// 2. Ghi một vài file TypeScript mẫu để kiểm tra
// File domain/entities/user.ts - vi phạm vì import @nestjs/common và dùng 'any'
const domainUserContent = `
import { Injectable } from '@nestjs/common';
import { InfrastructureDb } from '../infrastructure/db';

export class User {
  id: string;
  data: any; // Vi phạm allow_any: false
  role: string = 'admin'; // Vi phạm role_casing: UPPERCASE
}
`;
const domainPath = path.join(tempDir, 'src', 'domain', 'entities');
fs.mkdirSync(domainPath, { recursive: true });
fs.writeFileSync(path.join(domainPath, 'user.ts'), domainUserContent, 'utf-8');

// 3. Tiến hành kiểm tra
try {
  const config = loadArchitectureConfig(tempDir);
  console.log('Parsed Config Layers:', JSON.stringify(config.layers, null, 2));
  console.log('Parsed Config Conventions:', JSON.stringify(config.conventions, null, 2));
  const result = checkArchitecture(['src/domain/entities/user.ts'], config, tempDir);

  console.log('Result Status:', result.success);
  console.log('Violations Count:', result.violations.length);
  
  // In ra các vi phạm để đối chiếu
  result.violations.forEach(v => {
    console.log(`- [${v.rule}] File: ${v.file}, Line: ${v.line}, Msg: ${v.message}`);
  });

  // Verify các vi phạm cụ thể
  const rulesViolated = result.violations.map(v => v.rule);
  const hasDomainPurity = rulesViolated.includes('domain-purity');
  const hasNoAny = rulesViolated.includes('no-explicit-any');
  const hasRoleCasing = rulesViolated.includes('role-casing');

  if (hasDomainPurity && hasNoAny && hasRoleCasing && !result.success) {
    console.log('✅ TEST PASSED: Tất cả các vi phạm (Clean Architecture, any, Role uppercase) đều được phát hiện chính xác!');
    cleanup();
    process.exit(0);
  } else {
    console.error('❌ TEST FAILED: Không phát hiện đủ vi phạm kiến trúc mong đợi.');
    cleanup();
    process.exit(1);
  }
} catch (error) {
  console.error('❌ ERROR RUNNING TEST:', error);
  cleanup();
  process.exit(1);
}

function cleanup() {
  // Xóa thư mục tạm sau khi chạy test
  if (fs.existsSync(tempDir)) {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}