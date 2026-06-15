import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'

import { getOpenApiTypesCommand } from './check-api-types.mjs'

test('runs openapi-typescript through the current JavaScript runtime', () => {
  const { command, args } = getOpenApiTypesCommand('/tmp/openapi.json')

  assert.equal(command, process.execPath)
  assert.ok(args[0].endsWith(path.join('node_modules', 'openapi-typescript', 'bin', 'cli.js')))
  assert.equal(args[1], '/tmp/openapi.json')
})
