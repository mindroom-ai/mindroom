// Minimal Supabase JWT auth for an instance Express backend.
// Copy this file into your instance backend and use `verifyUser`
// as middleware for protected routes.

const { createClient } = require('@supabase/supabase-js')

const SUPABASE_URL = process.env.SUPABASE_URL
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY
const ACCOUNT_ID = process.env.ACCOUNT_ID // The owner of this instance

if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
  throw new Error('SUPABASE_URL and SUPABASE_ANON_KEY must be set')
}

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)

async function verifyUser(req, res, next) {
  const auth = req.headers['authorization'] || ''
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : ''
  if (!token) return res.status(401).json({ error: 'Missing token' })

  const { data: { user }, error } = await supabase.auth.getUser(token)
  if (error || !user) return res.status(401).json({ error: 'Invalid token' })

  if (ACCOUNT_ID && user.id !== ACCOUNT_ID) return res.status(403).json({ error: 'Forbidden' })

  req.user = user
  next()
}

module.exports = { verifyUser }
