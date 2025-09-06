import { Router } from 'express';
import { dokku } from '../utils/dokku.js';
import { supabase } from '../utils/supabase.js';
import { logger } from '../utils/logger.js';

const router = Router();

router.get('/', async (req, res) => {
  const health = {
    status: 'healthy',
    service: 'dokku-provisioner',
    timestamp: new Date().toISOString(),
    uptime: process.uptime(),
    environment: process.env.NODE_ENV || 'development'
  };

  // Check Dokku connectivity
  try {
    await dokku.connect();
    await dokku.execute('dokku version');
    await dokku.disconnect();
    health.dokku = 'connected';
  } catch (error) {
    health.dokku = 'disconnected';
    health.status = 'degraded';
    logger.error('Dokku health check failed:', error);
  }

  // Check Supabase connectivity
  try {
    const { error } = await supabase.from('instances').select('count').limit(1);
    if (error) throw error;
    health.supabase = 'connected';
  } catch (error) {
    health.supabase = 'disconnected';
    health.status = 'degraded';
    logger.error('Supabase health check failed:', error);
  }

  const statusCode = health.status === 'healthy' ? 200 : 503;
  res.status(statusCode).json(health);
});

export const healthRouter = router;
