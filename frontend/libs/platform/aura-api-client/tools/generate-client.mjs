import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(here, '../../../../..');
const contractPath = resolve(repositoryRoot, 'contracts/openapi/aura-v1.yaml');
const outputPath = resolve(here, '../src/lib/generated/aura-api.ts');
const contractText = readFileSync(contractPath, 'utf8');
const document = JSON.parse(contractText);
const schemas = document.components.schemas;

function refName(ref) {
  return ref.slice(ref.lastIndexOf('/') + 1);
}

function typeExpression(schema) {
  if (schema.$ref) return refName(schema.$ref);
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map((value) => JSON.stringify(value)).join(' | ');
  if (schema.oneOf) return schema.oneOf.map(typeExpression).join(' | ');
  if (schema.allOf) return schema.allOf.map(typeExpression).join(' & ');
  if (Array.isArray(schema.type)) return schema.type.map((value) => typeExpression({ ...schema, type: value })).join(' | ');
  if (schema.type === 'array') return `ReadonlyArray<${typeExpression(schema.items)}>`;
  if (schema.type === 'object') {
    if (schema.additionalProperties && typeof schema.additionalProperties === 'object' && !schema.properties) {
      return `Readonly<Record<string, ${typeExpression(schema.additionalProperties)}>>`;
    }
    const required = new Set(schema.required ?? []);
    const properties = Object.entries(schema.properties ?? {}).map(([name, value]) =>
      `readonly ${JSON.stringify(name)}${required.has(name) ? '' : '?'}: ${typeExpression(value)};`,
    );
    return `{ ${properties.join(' ')} }`;
  }
  if (schema.type === 'integer' || schema.type === 'number') return 'number';
  if (schema.type === 'boolean') return 'boolean';
  if (schema.type === 'null') return 'null';
  return 'string';
}

function schemaDeclaration(name, schema) {
  return `export type ${name} = ${typeExpression(schema)};`;
}

function resolveParameter(parameter) {
  if (!parameter.$ref) return parameter;
  return document.components.parameters[refName(parameter.$ref)];
}

function responseType(operation) {
  const response = Object.entries(operation.responses).find(([status]) => /^2/.test(status))?.[1];
  const content = response?.content;
  const schema = content?.['application/json']?.schema ?? content?.['text/event-stream']?.schema;
  return schema ? typeExpression(schema) : 'void';
}

function bodyType(operation) {
  const schema = operation.requestBody?.content?.['application/json']?.schema;
  return schema ? typeExpression(schema) : 'never';
}

function parameterType(parameters, location) {
  const selected = parameters.map(resolveParameter).filter((parameter) => parameter.in === location);
  if (selected.length === 0) return 'Readonly<Record<string, never>>';
  return `{ ${selected.map((parameter) => `readonly ${JSON.stringify(parameter.name)}${parameter.required ? '' : '?'}: ${typeExpression(parameter.schema)};`).join(' ')} }`;
}

const operations = [];
for (const [path, pathItem] of Object.entries(document.paths)) {
  for (const method of ['get', 'post', 'put', 'patch', 'delete']) {
    const operation = pathItem[method];
    if (!operation) continue;
    const parameters = [...(pathItem.parameters ?? []), ...(operation.parameters ?? [])];
    operations.push({
      id: operation.operationId,
      method: method.toUpperCase(),
      path,
      response: responseType(operation),
      body: bodyType(operation),
      pathType: parameterType(parameters, 'path'),
      queryType: parameterType(parameters, 'query'),
      headerType: parameterType(parameters, 'header'),
      mode: operation.responses['200']?.content?.['text/event-stream'] ? 'event-stream' : 'json',
    });
  }
}

const digest = createHash('sha256').update(contractText).digest('hex');
const operationTypes = operations.map((operation) =>
  `  readonly ${operation.id}: { readonly response: ${operation.response}; readonly body: ${operation.body}; readonly path: ${operation.pathType}; readonly query: ${operation.queryType}; readonly headers: ${operation.headerType}; };`,
).join('\n');
const descriptors = operations.map((operation) =>
  `  ${operation.id}: { method: '${operation.method}', pathTemplate: '${operation.path}', responseMode: '${operation.mode}' },`,
).join('\n');
const generated = `// Generated from contracts/openapi/aura-v1.yaml. Do not edit by hand.\n// Contract SHA-256: ${digest}\n\n${Object.entries(schemas).map(([name, schema]) => schemaDeclaration(name, schema)).join('\n\n')}\n\nexport interface AuraApiOperations {\n${operationTypes}\n}\n\nexport type AuraOperationId = keyof AuraApiOperations;\nexport type AuraOperationInput<K extends AuraOperationId> = Readonly<{\n  path: AuraApiOperations[K]['path'];\n  query: AuraApiOperations[K]['query'];\n  headers: AuraApiOperations[K]['headers'];\n  body: AuraApiOperations[K]['body'];\n}>;\nexport type AuraOperationResponse<K extends AuraOperationId> = AuraApiOperations[K]['response'];\n\nexport interface AuraOperationDescriptor {\n  readonly method: string;\n  readonly pathTemplate: string;\n  readonly responseMode: 'json' | 'event-stream';\n}\n\nexport const AURA_API_OPERATIONS = {\n${descriptors}\n} as const satisfies Readonly<Record<AuraOperationId, AuraOperationDescriptor>>;\n`;

if (process.argv.includes('--check')) {
  const current = readFileSync(outputPath, 'utf8');
  if (current !== generated) {
    process.stderr.write('Generated Aura API client is out of sync. Run this script without --check and apply the emitted output.\n');
    process.exitCode = 1;
  }
} else {
  process.stdout.write(generated);
}
