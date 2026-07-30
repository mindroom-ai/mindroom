import assert from 'node:assert/strict'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { getOpenApiTypesCommand } from './check-api-types.mjs'

test('runs openapi-typescript through the current JavaScript runtime', () => {
  const { command, args } = getOpenApiTypesCommand('/tmp/openapi.json')
  const expectedCliPath = fileURLToPath(new URL('bin/cli.js', import.meta.resolve('openapi-typescript/package.json')))

  assert.equal(command, process.execPath)
  assert.equal(args[0], expectedCliPath)
  assert.equal(args[1], '/tmp/openapi.json')
})
