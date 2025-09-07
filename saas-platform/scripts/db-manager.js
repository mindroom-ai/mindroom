#!/usr/bin/env node

/**
 * Unified Database Management Tool
 * This is the ONLY script you need for database operations
 */

const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
require('dotenv').config();

// Colors for terminal output
const colors = {
  reset: '\x1b[0m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m',
};

// Check environment
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY;
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY;
const SUPABASE_DB_PASSWORD = process.env.SUPABASE_DB_PASSWORD;

if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) {
  console.error(`${colors.red}❌ Missing required environment variables:${colors.reset}`);
  console.error('   SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY, {
  auth: {
    autoRefreshToken: false,
    persistSession: false
  }
});

// Command handlers
const commands = {
  /**
   * Show available migrations and instructions
   */
  async status() {
    console.log(`${colors.cyan}📊 Database Status${colors.reset}`);
    console.log('==================\n');

    // Check what tables exist
    const tables = await checkTables();
    const expectedTables = ['accounts', 'subscriptions', 'instances', 'usage_metrics', 'webhook_events', 'audit_logs'];

    console.log('Tables:');
    for (const table of expectedTables) {
      if (tables.includes(table)) {
        console.log(`  ${colors.green}✅${colors.reset} ${table}`);
      } else {
        console.log(`  ${colors.red}❌${colors.reset} ${table} (missing)`);
      }
    }

    // Check RLS status
    console.log('\nSecurity:');
    const rlsEnabled = await checkRLS();
    if (rlsEnabled) {
      console.log(`  ${colors.green}✅${colors.reset} Row Level Security enabled`);
    } else {
      console.log(`  ${colors.yellow}⚠️${colors.reset}  Row Level Security NOT enabled`);
      console.log(`     Run: ${colors.cyan}./db.sh apply${colors.reset}`);
    }

    // Show available migrations
    console.log('\nMigrations:');
    const migrations = getMigrations();
    migrations.forEach(m => {
      console.log(`  ${m.applied ? colors.green + '✅' : colors.yellow + '⏳'}${colors.reset} ${m.file}`);
    });
  },

  /**
   * Apply migrations (show SQL for manual application)
   */
  async apply() {
    console.log(`${colors.cyan}📋 Database Migrations${colors.reset}`);
    console.log('=====================\n');

    const migrations = getMigrations();

    console.log('To apply migrations, run these in Supabase Dashboard SQL Editor:\n');
    console.log('━'.repeat(60));

    for (const migration of migrations) {
      if (!migration.applied) {
        console.log(`\n${colors.yellow}-- File: ${migration.path}${colors.reset}`);
        console.log('━'.repeat(60));
        console.log(migration.content);
        console.log('━'.repeat(60));
      }
    }

    console.log(`\n${colors.cyan}After applying, run: ./db.sh status${colors.reset}`);
  },

  /**
   * Reset database (delete all data)
   */
  async reset() {
    if (!process.argv.includes('--force')) {
      console.log(`${colors.yellow}⚠️  WARNING: This will DELETE ALL DATA!${colors.reset}`);
      console.log(`Run with --force to confirm: ${colors.cyan}./db.sh reset --force${colors.reset}`);
      process.exit(1);
    }

    console.log(`${colors.red}🗑️  Resetting database...${colors.reset}\n`);

    const tables = ['audit_logs', 'webhook_events', 'usage_metrics', 'instances', 'subscriptions', 'accounts'];

    for (const table of tables) {
      try {
        const { error } = await supabase
          .from(table)
          .delete()
          .neq('id', '00000000-0000-0000-0000-000000000000');

        if (error && error.code !== '42P01') {
          console.log(`  ${colors.yellow}⚠️${colors.reset}  ${table}: ${error.message}`);
        } else {
          console.log(`  ${colors.green}✅${colors.reset} Cleared ${table}`);
        }
      } catch (e) {
        console.log(`  ${colors.red}❌${colors.reset} ${table}: ${e.message}`);
      }
    }

    console.log(`\n${colors.green}✅ Database reset complete${colors.reset}`);
    console.log(`Run ${colors.cyan}./db.sh apply${colors.reset} to reapply schema`);
  },

  /**
   * Setup Stripe products
   */
  async stripe() {
    console.log(`${colors.cyan}💳 Setting up Stripe products...${colors.reset}\n`);

    const setupScript = path.join(__dirname, 'db', 'setup-stripe-products.js');
    if (!fs.existsSync(setupScript)) {
      console.error(`${colors.red}❌ Stripe setup script not found${colors.reset}`);
      process.exit(1);
    }

    const child = spawn('node', [setupScript], { stdio: 'inherit' });
    child.on('exit', (code) => {
      if (code === 0) {
        console.log(`\n${colors.green}✅ Stripe products created${colors.reset}`);
      } else {
        console.error(`${colors.red}❌ Stripe setup failed${colors.reset}`);
      }
    });
  },

  /**
   * Create admin user
   */
  async admin() {
    console.log(`${colors.cyan}👤 Creating admin user...${colors.reset}\n`);

    const adminScript = path.join(__dirname, 'db', 'create-admin-user.js');
    if (!fs.existsSync(adminScript)) {
      console.error(`${colors.red}❌ Admin setup script not found${colors.reset}`);
      process.exit(1);
    }

    const child = spawn('node', [adminScript], { stdio: 'inherit' });
    child.on('exit', (code) => {
      if (code === 0) {
        console.log(`\n${colors.green}✅ Admin user created${colors.reset}`);
      } else {
        console.error(`${colors.red}❌ Admin setup failed${colors.reset}`);
      }
    });
  },

  /**
   * Show help
   */
  async help() {
    console.log(`${colors.cyan}🗄️  Database Manager${colors.reset}`);
    console.log('===================\n');
    console.log('Commands:');
    console.log(`  ${colors.green}status${colors.reset}  - Check database status and migrations`);
    console.log(`  ${colors.green}apply${colors.reset}   - Show SQL to apply migrations`);
    console.log(`  ${colors.green}reset${colors.reset}   - Delete all data (requires --force)`);
    console.log(`  ${colors.green}stripe${colors.reset}  - Setup Stripe products and pricing`);
    console.log(`  ${colors.green}admin${colors.reset}   - Create admin dashboard user`);
    console.log(`  ${colors.green}help${colors.reset}    - Show this help message`);
    console.log('\nUsage:');
    console.log(`  ./db.sh [command]`);
    console.log('\nExamples:');
    console.log(`  ./db.sh status`);
    console.log(`  ./db.sh reset --force`);
  }
};

// Helper functions
async function checkTables() {
  try {
    const response = await fetch(`${SUPABASE_URL}/rest/v1/`, {
      headers: {
        'apikey': SUPABASE_SERVICE_KEY,
        'Authorization': `Bearer ${SUPABASE_SERVICE_KEY}`
      }
    });
    const data = await response.json();
    return Object.keys(data.definitions || {});
  } catch (e) {
    return [];
  }
}

async function checkRLS() {
  try {
    // Try to access with anon key
    const response = await fetch(`${SUPABASE_URL}/rest/v1/accounts?limit=1`, {
      headers: {
        'apikey': SUPABASE_ANON_KEY
      }
    });
    const data = await response.json();
    // If we get an empty array, RLS might not be enabled
    // If we get an error, RLS is likely enabled
    return !Array.isArray(data) || data.message;
  } catch (e) {
    return true; // Error likely means RLS is blocking
  }
}

function getMigrations() {
  const migrationsDir = path.join(__dirname, '..', 'supabase', 'migrations');
  const files = fs.readdirSync(migrationsDir)
    .filter(f => f.endsWith('.sql'))
    .sort();

  return files.map(file => {
    const content = fs.readFileSync(path.join(migrationsDir, file), 'utf8');
    const applied = file === '001_schema.sql' ? checkTables().length > 0 : false;
    return {
      file,
      path: `supabase/migrations/${file}`,
      content: content.substring(0, 500) + (content.length > 500 ? '\n...' : ''),
      applied
    };
  });
}

// Main execution
async function main() {
  const command = process.argv[2] || 'help';

  if (commands[command]) {
    try {
      await commands[command]();
    } catch (error) {
      console.error(`${colors.red}❌ Error: ${error.message}${colors.reset}`);
      process.exit(1);
    }
  } else {
    console.error(`${colors.red}❌ Unknown command: ${command}${colors.reset}`);
    await commands.help();
    process.exit(1);
  }
}

main();
