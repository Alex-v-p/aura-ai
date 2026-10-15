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

const authLogin = document.paths?.['/api/v1/auth/login']?.get;
assert(authLogin?.operationId === 'startOwnerLogin', 'Owner login operation is missing or renamed');
assert(authLogin.security?.length === 0, 'Owner login initiation must be anonymous');
assert(
  !document.paths?.['/api/v1/auth/callback'],
  'Provider-facing callback must remain outside the generated browser contract',
);
const returnTo = authLogin.parameters?.find((parameter) => parameter.name === 'return_to');
assert(returnTo?.in === 'query' && !returnTo.required, 'Owner login return_to must be an optional query value');
assert(returnTo?.schema?.pattern === '^/(?:$|[^/].*)$', 'Owner login return_to must remain same-origin');
assert(returnTo?.schema?.maxLength === 2048, 'Owner login return_to must remain bounded');
assert(returnTo?.schema?.default === '/', 'Owner login return_to must default to the shell root');
assert(
  authLogin.responses?.['307']?.headers?.Location?.schema?.format === 'uri',
  'Owner login must declare its temporary redirect Location',
);

const authMe = document.paths?.['/api/v1/auth/me']?.get;
assert(authMe?.operationId === 'getOwnerSession', 'Owner-session operation is missing or renamed');
assert(
  authMe.responses?.['200']?.content?.['application/json']?.schema?.$ref ===
    '#/components/schemas/OwnerSession',
  'Owner-session response must use the generated OwnerSession schema',
);
assert(authMe.responses?.['401'], 'Owner-session lookup must declare unauthenticated access');

const authLogout = document.paths?.['/api/v1/auth/logout']?.post;
assert(authLogout?.operationId === 'logoutOwnerSession', 'Owner logout operation is missing or renamed');
assert(!authLogout.requestBody, 'Owner logout must not accept a request body');
assert(
  authLogout.responses?.['204'] && !authLogout.responses['204'].content,
  'Owner logout must return an empty 204 response',
);
assert(authLogout.responses?.['403'], 'Owner logout must declare anti-CSRF denial');
assert(
  authLogout.parameters?.some((parameter) => parameter.$ref === '#/components/parameters/CsrfToken'),
  'Owner logout must require the shared X-Observatory-CSRF header',
);

const ownerSession = document.components.schemas.OwnerSession;
assert(ownerSession?.type === 'object', 'OwnerSession must be an object schema');
assert(ownerSession.additionalProperties === false, 'OwnerSession must reject undeclared fields');
assert(
  ownerSession.required?.length === 1 && ownerSession.required[0] === 'csrfToken',
  'OwnerSession must expose only the anti-CSRF value',
);
assert(
  Object.keys(ownerSession.properties ?? {}).length === 1,
  'OwnerSession must not expose owner identity or arbitrary claims',
);
assert(ownerSession.properties?.csrfToken?.minLength === 32, 'Owner CSRF token must preserve minimum entropy');
assert(ownerSession.properties?.csrfToken?.maxLength === 256, 'Owner CSRF token must remain bounded');

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
