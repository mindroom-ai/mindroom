import { Router } from 'express';
import { z } from 'zod';
import { dokku } from '../utils/dokku.js';
import { supabase, updateInstanceStatus } from '../utils/supabase.js';
import { logger } from '../utils/logger.js';

const router = Router();

// Start an instance
router.post('/:instanceId/start', async (req, res) => {
  try {
    const { instanceId } = req.params;

    // Get instance details
    const { data: instance, error } = await supabase
      .from('instances')
      .select('*')
      .eq('id', instanceId)
      .single();

    if (error || !instance) {
      return res.status(404).json({ error: 'Instance not found' });
    }

    await dokku.connect();

    // Start all apps
    const apps = [
      instance.dokku_app_name,
      `${instance.dokku_app_name}-backend`,
      `${instance.dokku_app_name}-frontend`,
      `${instance.dokku_app_name}-matrix`
    ];

    for (const app of apps) {
      await dokku.startApp(app);
    }

    await updateInstanceStatus(instanceId, 'running');
    await dokku.disconnect();

    res.json({ success: true, message: 'Instance started' });
  } catch (error) {
    logger.error('Failed to start instance:', error);
    res.status(500).json({ error: 'Failed to start instance' });
  }
});

// Stop an instance
router.post('/:instanceId/stop', async (req, res) => {
  try {
    const { instanceId } = req.params;

    // Get instance details
    const { data: instance, error } = await supabase
      .from('instances')
      .select('*')
      .eq('id', instanceId)
      .single();

    if (error || !instance) {
      return res.status(404).json({ error: 'Instance not found' });
    }

    await dokku.connect();

    // Stop all apps
    const apps = [
      instance.dokku_app_name,
      `${instance.dokku_app_name}-backend`,
      `${instance.dokku_app_name}-frontend`,
      `${instance.dokku_app_name}-matrix`
    ];

    for (const app of apps) {
      await dokku.stopApp(app);
    }

    await updateInstanceStatus(instanceId, 'stopped');
    await dokku.disconnect();

    res.json({ success: true, message: 'Instance stopped' });
  } catch (error) {
    logger.error('Failed to stop instance:', error);
    res.status(500).json({ error: 'Failed to stop instance' });
  }
});

// Restart an instance
router.post('/:instanceId/restart', async (req, res) => {
  try {
    const { instanceId } = req.params;

    // Get instance details
    const { data: instance, error } = await supabase
      .from('instances')
      .select('*')
      .eq('id', instanceId)
      .single();

    if (error || !instance) {
      return res.status(404).json({ error: 'Instance not found' });
    }

    await dokku.connect();

    // Restart all apps
    const apps = [
      instance.dokku_app_name,
      `${instance.dokku_app_name}-backend`,
      `${instance.dokku_app_name}-frontend`,
      `${instance.dokku_app_name}-matrix`
    ];

    for (const app of apps) {
      await dokku.restartApp(app);
    }

    await updateInstanceStatus(instanceId, 'running');
    await dokku.disconnect();

    res.json({ success: true, message: 'Instance restarted' });
  } catch (error) {
    logger.error('Failed to restart instance:', error);
    res.status(500).json({ error: 'Failed to restart instance' });
  }
});

// Get instance status
router.get('/:instanceId/status', async (req, res) => {
  try {
    const { instanceId } = req.params;

    // Get instance details
    const { data: instance, error } = await supabase
      .from('instances')
      .select('*')
      .eq('id', instanceId)
      .single();

    if (error || !instance) {
      return res.status(404).json({ error: 'Instance not found' });
    }

    res.json({
      id: instance.id,
      status: instance.status,
      subdomain: instance.subdomain,
      frontend_url: instance.frontend_url,
      backend_url: instance.backend_url,
      matrix_server_url: instance.matrix_server_url,
      created_at: instance.created_at,
      updated_at: instance.updated_at
    });
  } catch (error) {
    logger.error('Failed to get instance status:', error);
    res.status(500).json({ error: 'Failed to get instance status' });
  }
});

// Scale instance resources
router.post('/:instanceId/scale', async (req, res) => {
  try {
    const { instanceId } = req.params;
    const { memory, cpu } = req.body;

    // Validate inputs
    if (!memory && !cpu) {
      return res.status(400).json({ error: 'Memory or CPU must be specified' });
    }

    // Get instance details
    const { data: instance, error } = await supabase
      .from('instances')
      .select('*')
      .eq('id', instanceId)
      .single();

    if (error || !instance) {
      return res.status(404).json({ error: 'Instance not found' });
    }

    await dokku.connect();

    // Update resource limits for all apps
    const apps = [
      instance.dokku_app_name,
      `${instance.dokku_app_name}-backend`,
      `${instance.dokku_app_name}-frontend`,
      `${instance.dokku_app_name}-matrix`
    ];

    const limits = {};
    if (memory) limits.memory = memory;
    if (cpu) limits.cpu = cpu;

    for (const app of apps) {
      await dokku.setResourceLimits(app, limits);
    }

    // Update config in database
    await supabase
      .from('instances')
      .update({
        config: {
          ...instance.config,
          resources: limits
        },
        updated_at: new Date().toISOString()
      })
      .eq('id', instanceId);

    await dokku.disconnect();

    res.json({ success: true, message: 'Instance scaled successfully' });
  } catch (error) {
    logger.error('Failed to scale instance:', error);
    res.status(500).json({ error: 'Failed to scale instance' });
  }
});

export const instanceRouter = router;
