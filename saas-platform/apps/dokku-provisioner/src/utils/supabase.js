import { createClient } from '@supabase/supabase-js';
import { logger } from './logger.js';

if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_KEY) {
  logger.error('Missing Supabase configuration');
  throw new Error('SUPABASE_URL and SUPABASE_SERVICE_KEY are required');
}

export const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_KEY,
  {
    auth: {
      autoRefreshToken: false,
      persistSession: false
    }
  }
);

// Helper function to update instance status
export async function updateInstanceStatus(instanceId, status, additionalData = {}) {
  const { error } = await supabase
    .from('instances')
    .update({
      status,
      ...additionalData,
      updated_at: new Date().toISOString()
    })
    .eq('id', instanceId);

  if (error) {
    logger.error(`Failed to update instance ${instanceId} status to ${status}:`, error);
    throw error;
  }

  logger.info(`Instance ${instanceId} status updated to ${status}`);
  return true;
}
