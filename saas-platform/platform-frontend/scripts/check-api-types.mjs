// Fails when src/lib/api.generated.ts is stale relative to the committed OpenAPI schema.
// Run with `bun run check:api`; regenerate with `just saas-openapi` (or `bun run generate:api`).
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const schemaPath = fileURLToPath(new URL('../../platform-backend/openapi.json', import.meta.url))
const generatedPath = fileURLToPath(new URL('../src/lib/api.generated.ts', import.meta.url))
const cliPath = fileURLToPath(new URL('../node_modules/.bin/openapi-typescript', import.meta.url))

const fresh = execFileSync(cliPath, [schemaPath], { encoding: 'utf8' })
const committed = readFileSync(generatedPath, 'utf8')

if (fresh !== committed) {
  console.error(
    'src/lib/api.generated.ts is stale relative to platform-backend/openapi.json.\n' +
      'Run `just saas-openapi` from the repo root (or `bun run generate:api` here) and commit the result.',
  )
  process.exit(1)
}
console.log('api.generated.ts is up to date.')
