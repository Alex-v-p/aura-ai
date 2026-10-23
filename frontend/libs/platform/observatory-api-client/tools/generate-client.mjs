import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(here, '../../../../..');
const contractPath = resolve(repositoryRoot, 'contracts/openapi/observatory-v1.yaml');
const outputPath = resolve(here, '../src/lib/generated/observatory-api.ts');
const contractText = readFileSync(contractPath, 'utf8');
const document = JSON.parse(contractText);
const schemas = document.components.schemas;

function refName(ref) {
  return ref.slice(ref.lastIndexOf('/') + 1);
}

function typeExpression(schema) {
  if (schema.$ref) return refName(schema.$ref);
  if (schema.type === 'string' && schema.format === 'binary') return 'Blob';
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map((value) => JSON.stringify(value)).join(' | ');
  if (schema.oneOf && schema.type !== 'object') return schema.oneOf.map(typeExpression).join(' | ');
  if (schema.allOf) return schema.allOf.map(typeExpression).join(' & ');
  if (Array.isArray(schema.type)) {
    return schema.type.map((value) => typeExpression({ ...schema, enum: undefined, type: value })).join(' | ');
  }
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

function resolveParameter(parameter) {
  if (!parameter.$ref) return parameter;
  return document.components.parameters[refName(parameter.$ref)];
}

function resolveResponse(response) {
  if (!response?.$ref) return response;
  const prefix = '#/components/responses/';
  if (!response.$ref.startsWith(prefix)) {
    throw new Error(`Unsupported response reference: ${response.$ref}`);
  }
  const resolved = document.components.responses?.[refName(response.$ref)];
  if (!resolved) throw new Error(`Missing response reference: ${response.$ref}`);
  return resolved;
}

function successResponse(operation) {
  const response = Object.entries(operation.responses).find(([status]) => /^2/.test(status))?.[1];
  return resolveResponse(response);
}

function assertSupportedContent(content, allowed, context) {
  if (!content) return;
  const unsupported = Object.keys(content).filter((mediaType) => !allowed.includes(mediaType));
  if (unsupported.length > 0) throw new Error(`Unsupported ${context} media type(s): ${unsupported.join(', ')}`);
  if (allowed.filter((mediaType) => content[mediaType]).length > 1) throw new Error(`Ambiguous ${context} media types`);
}

function responseType(operation) {
  const content = successResponse(operation)?.content;
  assertSupportedContent(content, ['application/json', 'text/event-stream'], `${operation.operationId} response`);
  const schema = content?.['application/json']?.schema ?? content?.['text/event-stream']?.schema;
  return schema ? typeExpression(schema) : 'void';
}

function bodyType(operation) {
  const content = operation.requestBody?.content;
  assertSupportedContent(content, ['application/json', 'multipart/form-data'], `${operation.operationId} request`);
  const schema = content?.['application/json']?.schema ?? content?.['multipart/form-data']?.schema;
  return schema ? typeExpression(schema) : 'never';
}

function parameterType(parameters, location) {
  const selected = parameters.map(resolveParameter).filter((parameter) => parameter.in === location);
  if (selected.length === 0) return 'Readonly<Record<string, never>>';
  return `{ ${selected.map((parameter) => `readonly ${JSON.stringify(parameter.name)}${parameter.required ? '' : '?'}: ${location === 'header' ? 'string' : typeExpression(parameter.schema)};`).join(' ')} }`;
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
      requestMode: operation.requestBody?.content?.['multipart/form-data']
        ? 'multipart'
        : operation.requestBody
          ? 'json'
          : 'none',
      responseMode: successResponse(operation)?.content?.['text/event-stream']
        ? 'event-stream'
        : successResponse(operation)?.content
          ? 'json'
          : 'none',
    });
  }
}

const digest = createHash('sha256').update(contractText).digest('hex');
const operationTypes = operations.map((operation) =>
  `  readonly ${operation.id}: { readonly response: ${operation.response}; readonly body: ${operation.body}; readonly path: ${operation.pathType}; readonly query: ${operation.queryType}; readonly headers: ${operation.headerType}; };`,
).join('\n');
const descriptors = operations.map((operation) =>
  `  ${operation.id}: { method: '${operation.method}', pathTemplate: '${operation.path}', requestMode: '${operation.requestMode}', responseMode: '${operation.responseMode}' },`,
).join('\n');
const generated = `// Generated from contracts/openapi/observatory-v1.yaml. Do not edit by hand.\n// Contract SHA-256: ${digest}\n\n${Object.entries(schemas).map(([name, schema]) => `export type ${name} = ${typeExpression(schema)};`).join('\n\n')}\n\nexport interface ObservatoryApiOperations {\n${operationTypes}\n}\n\nexport type ObservatoryOperationId = keyof ObservatoryApiOperations;\nexport type ObservatoryOperationInput<K extends ObservatoryOperationId> = Readonly<{\n  path: ObservatoryApiOperations[K]['path'];\n  query: ObservatoryApiOperations[K]['query'];\n  headers: ObservatoryApiOperations[K]['headers'];\n  body: ObservatoryApiOperations[K]['body'];\n}>;\nexport type ObservatoryOperationResponse<K extends ObservatoryOperationId> = ObservatoryApiOperations[K]['response'];\n\nexport interface ObservatoryOperationDescriptor {\n  readonly method: string;\n  readonly pathTemplate: string;\n  readonly requestMode: 'none' | 'json' | 'multipart';\n  readonly responseMode: 'none' | 'json' | 'event-stream';\n}\n\nexport const OBSERVATORY_API_OPERATIONS = {\n${descriptors}\n} as const satisfies Readonly<Record<ObservatoryOperationId, ObservatoryOperationDescriptor>>;\n`;

if (process.argv.includes('--check')) {
  if (readFileSync(outputPath, 'utf8') !== generated) {
    process.stderr.write('Generated Observatory API client is out of sync. Run this script without --check.\n');
    process.exitCode = 1;
  }
} else {
  writeFileSync(outputPath, generated);
}
