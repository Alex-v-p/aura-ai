import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(here, '../../../../..');
const contractPath = resolve(repositoryRoot, 'contracts/openapi/observatory-v1.yaml');
const document = JSON.parse(readFileSync(contractPath, 'utf8'));

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function localRef(ref) {
  const prefix = '#/components/schemas/';
  assert(typeof ref === 'string' && ref.startsWith(prefix), `Unsupported schema reference: ${ref}`);
  const name = ref.slice(prefix.length);
  const schema = document.components.schemas[name];
  assert(schema, `Missing response schema: ${name}`);
  return schema;
}

assert(document.openapi === '3.1.0', 'Observatory contract must remain OpenAPI 3.1.0');
assert(document.info?.['x-contract-id'] === 'observatory-v1', 'Contract id drifted');
assert(document.info?.version === '1.0.0', 'Contract version drifted');

for (const [path, pathItem] of Object.entries(document.paths)) {
  for (const method of ['get', 'post', 'put', 'patch', 'delete']) {
    const operation = pathItem[method];
    if (!operation) continue;
    for (const [status, response] of Object.entries(operation.responses)) {
      if (!/^2/.test(status)) continue;
      for (const media of Object.values(response.content ?? {})) {
        if (media.schema?.$ref) localRef(media.schema.$ref);
      }
    }
  }
}

// A closed summary cannot be extended through allOf: JSON Schema evaluates
// the detail-only fields against the summary's additionalProperties: false.
// Keep the detail response independently closed and self-contained.
const runDetail = document.components.schemas.RunDetail;
assert(runDetail.type === 'object', 'RunDetail must be a direct object schema');
assert(runDetail.additionalProperties === false, 'RunDetail must reject undeclared fields');
assert(!runDetail.allOf, 'RunDetail must not extend a closed schema through allOf');
const required = new Set(runDetail.required ?? []);
assert(required.has('observations'), 'RunDetail must require observations');
assert(required.has('partial'), 'RunDetail must disclose bounded partial responses');
assert(required.has('nextCursor'), 'RunDetail must expose an opaque continuation cursor');
for (const name of required) {
  assert(runDetail.properties?.[name], `RunDetail required property is undeclared: ${name}`);
}
assert(
  runDetail.properties.observations?.maxItems === 10000,
  'RunDetail observations must remain explicitly bounded',
);

const traceDetail = document.components.schemas.TraceDetail;
assert(traceDetail.type === 'object', 'TraceDetail must be a direct object schema');
assert(traceDetail.additionalProperties === false, 'TraceDetail must reject undeclared fields');
assert(traceDetail.required?.includes('partial'), 'TraceDetail must disclose partial topology');
assert(traceDetail.required?.includes('nextCursor'), 'TraceDetail must expose continuation');
assert(traceDetail.properties.spans?.maxItems === 10000, 'TraceDetail spans must remain bounded');

process.stdout.write('Observatory OpenAPI response-schema invariants passed.\n');
