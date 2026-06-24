/**
 * STAX Performance Baseline — k6 Script
 * ======================================
 * Đây là kịch bản k6 chính thức để đo baseline performance.
 * Yêu cầu: k6 >= 0.49.0 (https://k6.io/docs/getting-started/installation/)
 *
 * Usage: k6 run tools/kaos/benchmarks/module-benchmark.js
 *
 * Các metrics được thu thập:
 *   - http_req_duration (p50, p95, p99)
 *   - http_reqs (RPS)
 *   - iterations (throughput)
 *   - checks (tỷ lệ pass kiểm tra)
 */

import http from 'k6/http';
import { check, sleep } from 'k6';

// Module test: Chọn module qua biến môi trường MODULE
const moduleName = __ENV.MODULE || 'crm';

const endpoints = {
  crm: {
    base: 'http://localhost:3000/api/v1/crm',
    paths: [
      { path: '/contacts', method: 'GET', weight: 5 },
      { path: '/leads', method: 'GET', weight: 3 },
    ],
  },
  accounting: {
    base: 'http://localhost:3000/api/v1/accounting',
    paths: [
      { path: '/receivables', method: 'GET', weight: 4 },
      { path: '/cash-flow', method: 'GET', weight: 2 },
    ],
  },
  employee: {
    base: 'http://localhost:3000/api/v1/employee',
    paths: [
      { path: '/list', method: 'GET', weight: 5 },
    ],
  },
};

const module = endpoints[moduleName] || endpoints.crm;

export const options = {
  stages: [
    { duration: '10s', target: 5 },    // Warm-up
    { duration: '10s', target: 20 },   // Ramp up
    { duration: '30s', target: 50 },   // Load test
    { duration: '5s', target: 0 },     // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],  // 95% requests < 500ms
    http_req_failed: ['rate<0.01'],    // < 1% failures
  },
};

export default function () {
  const baseUrl = __ENV.BASE_URL || module.base;

  // Chọn endpoint dựa trên weight
  const totalWeight = module.paths.reduce((s, p) => s + p.weight, 0);
  let rand = Math.random() * totalWeight;
  let chosen = module.paths[0];

  for (const p of module.paths) {
    rand -= p.weight;
    if (rand <= 0) {
      chosen = p;
      break;
    }
  }

  const url = `${baseUrl}${chosen.path}`;

  const params = {
    headers: {
      'Authorization': 'Bearer benchmark-token',
      'Content-Type': 'application/json',
    },
    tags: { endpoint: chosen.path },
  };

  const res = http.get(url, params);

  check(res, {
    'status is 200': (r) => r.status === 200,
    'response time < 500ms': (r) => r.timings.duration < 500,
  });

  sleep(0.1);
}
