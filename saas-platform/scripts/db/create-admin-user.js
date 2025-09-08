#!/usr/bin/env node

/**
 * Creates an admin user in Supabase for the admin dashboard
 * This script should be run after setting up Supabase
 */

const { createClient } = require('@supabase/supabase-js');
require('dotenv').config();

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY;
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || 'admin@mindroom.test';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'AdminPass123!';

if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) {
  console.error('Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY, {
  auth: {
    autoRefreshToken: false,
    persistSession: false
  }
});

async function createAdminUser() {
  try {
    console.log(`Creating admin user: ${ADMIN_EMAIL}`);

    // Check if user already exists
    const { data: existingUser } = await supabase.auth.admin.getUserById(ADMIN_EMAIL).catch(() => ({ data: null }));

    if (existingUser) {
      console.log('Admin user already exists, skipping creation');
      return;
    }

    // Create the admin user
    const { data: userData, error: createError } = await supabase.auth.admin.createUser({
      email: ADMIN_EMAIL,
      password: ADMIN_PASSWORD,
      email_confirm: true
    });

    if (createError) {
      // Check if it's a duplicate user error
      if (createError.message?.includes('already been registered')) {
        console.log('Admin user already exists');
        return;
      }
      throw createError;
    }

    console.log('✅ Admin user created successfully!');
    console.log(`   Email: ${ADMIN_EMAIL}`);
    console.log(`   Password: ${ADMIN_PASSWORD}`);
    console.log('   You can now login to the admin section at http://localhost:3000/admin');

  } catch (error) {
    console.error('Error creating admin user:', error.message);
    process.exit(1);
  }
}

createAdminUser();
