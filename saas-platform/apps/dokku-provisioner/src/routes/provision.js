import { Router } from 'express';
import { z } from 'zod';
import { provisioner } from '../services/provisioner.js';
import { logger } from '../utils/logger.js';

const router = Router();

// Validation schema for provision request
const provisionSchema = z.object({
  subscription_id: z.string().uuid(),
  account_id: z.string().uuid(),
  tier: z.enum(['starter', 'professional', 'enterprise']),
  limits: z.object({
    max_agents: z.number().int().positive(),
    max_messages_per_day: z.number().int().positive(),
    max_storage_gb: z.number().positive(),
    max_platforms: z.number().int().positive(),
    max_team_members: z.number().int().positive()
  })
});

// Provision a new instance
router.post('/', async (req, res) => {
  try {
    // Validate request body
    const validatedData = provisionSchema.parse(req.body);

    logger.info('Provision request received:', {
      subscription_id: validatedData.subscription_id,
      tier: validatedData.tier
    });

    // Start provisioning
    const result = await provisioner.provisionInstance(
      validatedData.subscription_id,
      validatedData.account_id,
      validatedData.tier,
      validatedData.limits
    );

    res.json(result);
  } catch (error) {
    if (error instanceof z.ZodError) {
      logger.warn('Invalid provision request:', error.errors);
      return res.status(400).json({
        error: 'Invalid request',
        details: error.errors
      });
    }

    logger.error('Provision request failed:', error);
    res.status(500).json({
      error: 'Provisioning failed',
      message: error.message
    });
  }
});

// Deprovision an instance
router.delete('/:instanceId', async (req, res) => {
  try {
    const { instanceId } = req.params;

    if (!instanceId || !z.string().uuid().safeParse(instanceId).success) {
      return res.status(400).json({
        error: 'Invalid instance ID'
      });
    }

    logger.info('Deprovision request received:', { instanceId });

    const result = await provisioner.deprovisionInstance(instanceId);

    res.json(result);
  } catch (error) {
    logger.error('Deprovision request failed:', error);
    res.status(500).json({
      error: 'Deprovisioning failed',
      message: error.message
    });
  }
});

export const provisionRouter = router;
