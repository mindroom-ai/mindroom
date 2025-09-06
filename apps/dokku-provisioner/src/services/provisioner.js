import { dokku } from '../utils/dokku.js';
import { supabase, updateInstanceStatus } from '../utils/supabase.js';
import { logger } from '../utils/logger.js';
import crypto from 'crypto';

export class ProvisionerService {
  async provisionInstance(subscriptionId, accountId, tier, limits) {
    const instanceId = crypto.randomUUID();
    const timestamp = Date.now();
    const subdomain = `${tier}-${timestamp}`;
    const appName = `mindroom-${accountId.substring(0, 8)}-${timestamp}`;

    logger.info(`Starting provisioning for subscription ${subscriptionId}`);
    logger.info(`Instance ID: ${instanceId}, App: ${appName}, Subdomain: ${subdomain}`);

    try {
      // Step 1: Create instance record in database
      const { error: insertError } = await supabase
        .from('instances')
        .insert({
          id: instanceId,
          subscription_id: subscriptionId,
          dokku_app_name: appName,
          subdomain,
          status: 'provisioning',
          config: {
            tier,
            limits,
            provisioned_at: new Date().toISOString()
          }
        });

      if (insertError) {
        throw new Error(`Failed to create instance record: ${insertError.message}`);
      }

      // Start async provisioning
      this.provisionAsync(instanceId, appName, subdomain, tier, limits)
        .catch(error => {
          logger.error(`Async provisioning failed for ${instanceId}:`, error);
          updateInstanceStatus(instanceId, 'failed', {
            error_message: error.message
          });
        });

      return {
        success: true,
        instanceId,
        appName,
        subdomain,
        message: 'Provisioning started'
      };

    } catch (error) {
      logger.error('Provisioning failed:', error);
      throw error;
    }
  }

  async provisionAsync(instanceId, appName, subdomain, tier, limits) {
    try {
      await dokku.connect();

      // Step 1: Create Dokku apps
      logger.info(`Creating Dokku apps for ${appName}...`);

      // Create main app
      await dokku.createApp(appName);

      // Create apps for each component
      const backendApp = `${appName}-backend`;
      const frontendApp = `${appName}-frontend`;
      const matrixApp = `${appName}-matrix`;

      await dokku.createApp(backendApp);
      await dokku.createApp(frontendApp);
      await dokku.createApp(matrixApp);

      // Step 2: Set up databases
      logger.info(`Setting up databases for ${appName}...`);

      // PostgreSQL for backend and Matrix
      const pgName = `${appName}-pg`;
      await dokku.createPostgres(pgName);
      await dokku.linkPostgres(backendApp, pgName);
      await dokku.linkPostgres(matrixApp, pgName);

      // Redis for caching
      const redisName = `${appName}-redis`;
      await dokku.createRedis(redisName);
      await dokku.linkRedis(backendApp, redisName);
      await dokku.linkRedis(matrixApp, redisName);

      // Step 3: Configure environment variables
      logger.info(`Configuring environment for ${appName}...`);

      // Backend config
      await dokku.setConfig(backendApp, {
        NODE_ENV: 'production',
        TIER: tier,
        MAX_AGENTS: limits.max_agents,
        MAX_MESSAGES_PER_DAY: limits.max_messages_per_day,
        INSTANCE_ID: instanceId,
        CORS_ORIGIN: `https://${subdomain}.${process.env.PLATFORM_DOMAIN}`
      });

      // Frontend config
      await dokku.setConfig(frontendApp, {
        NODE_ENV: 'production',
        VITE_API_URL: `https://api-${subdomain}.${process.env.PLATFORM_DOMAIN}`,
        VITE_MATRIX_URL: `https://matrix-${subdomain}.${process.env.PLATFORM_DOMAIN}`,
        VITE_INSTANCE_ID: instanceId
      });

      // Matrix config
      await dokku.setConfig(matrixApp, {
        SYNAPSE_SERVER_NAME: `matrix-${subdomain}.${process.env.PLATFORM_DOMAIN}`,
        SYNAPSE_REPORT_STATS: 'no',
        INSTANCE_ID: instanceId
      });

      // Step 4: Set up domains
      logger.info(`Setting up domains for ${appName}...`);

      const platformDomain = process.env.PLATFORM_DOMAIN || 'mindroom.app';

      await dokku.addDomain(frontendApp, `${subdomain}.${platformDomain}`);
      await dokku.addDomain(backendApp, `api-${subdomain}.${platformDomain}`);
      await dokku.addDomain(matrixApp, `matrix-${subdomain}.${platformDomain}`);

      // Step 5: Deploy Docker images
      logger.info(`Deploying applications for ${appName}...`);

      const registry = process.env.REGISTRY || 'ghcr.io/mindroom';

      // Deploy backend
      await dokku.deployDockerImage(backendApp, `${registry}/mindroom-backend:latest`);

      // Deploy frontend
      await dokku.deployDockerImage(frontendApp, `${registry}/mindroom-frontend:latest`);

      // Deploy Matrix/Synapse
      await dokku.deployDockerImage(matrixApp, 'matrixdotorg/synapse:latest');

      // Step 6: Enable SSL with Let's Encrypt
      logger.info(`Enabling SSL for ${appName}...`);

      await dokku.enableLetsencrypt(frontendApp);
      await dokku.enableLetsencrypt(backendApp);
      await dokku.enableLetsencrypt(matrixApp);

      // Step 7: Set resource limits based on tier
      logger.info(`Setting resource limits for ${appName}...`);

      const resourceLimits = this.getResourceLimits(tier);

      await dokku.setResourceLimits(backendApp, resourceLimits);
      await dokku.setResourceLimits(frontendApp, resourceLimits);
      await dokku.setResourceLimits(matrixApp, resourceLimits);

      // Step 8: Get URLs and update instance record
      logger.info(`Finalizing instance ${instanceId}...`);

      const frontendUrl = `https://${subdomain}.${platformDomain}`;
      const backendUrl = `https://api-${subdomain}.${platformDomain}`;
      const matrixUrl = `https://matrix-${subdomain}.${platformDomain}`;

      await updateInstanceStatus(instanceId, 'running', {
        frontend_url: frontendUrl,
        backend_url: backendUrl,
        matrix_server_url: matrixUrl
      });

      logger.info(`✅ Instance ${instanceId} provisioned successfully!`);
      logger.info(`   Frontend: ${frontendUrl}`);
      logger.info(`   Backend: ${backendUrl}`);
      logger.info(`   Matrix: ${matrixUrl}`);

    } catch (error) {
      logger.error(`Failed to provision instance ${instanceId}:`, error);
      await updateInstanceStatus(instanceId, 'failed', {
        error_message: error.message
      });

      // Attempt cleanup on failure
      try {
        await this.cleanupFailedProvisioning(appName);
      } catch (cleanupError) {
        logger.error('Cleanup failed:', cleanupError);
      }

      throw error;
    } finally {
      await dokku.disconnect();
    }
  }

  async cleanupFailedProvisioning(appName) {
    logger.info(`Cleaning up failed provisioning for ${appName}...`);

    try {
      await dokku.connect();

      // Destroy apps
      const apps = [
        appName,
        `${appName}-backend`,
        `${appName}-frontend`,
        `${appName}-matrix`
      ];

      for (const app of apps) {
        if (await dokku.appExists(app)) {
          await dokku.destroyApp(app);
        }
      }

      // Note: Dokku automatically cleans up linked databases when apps are destroyed

    } catch (error) {
      logger.error('Cleanup error:', error);
    } finally {
      await dokku.disconnect();
    }
  }

  getResourceLimits(tier) {
    const limits = {
      starter: {
        memory: '512m',
        cpu: '0.5'
      },
      professional: {
        memory: '2g',
        cpu: '1'
      },
      enterprise: {
        memory: '8g',
        cpu: '4'
      }
    };

    return limits[tier] || limits.starter;
  }

  async deprovisionInstance(instanceId) {
    logger.info(`Starting deprovisioning for instance ${instanceId}`);

    try {
      // Get instance details
      const { data: instance, error } = await supabase
        .from('instances')
        .select('*')
        .eq('id', instanceId)
        .single();

      if (error || !instance) {
        throw new Error('Instance not found');
      }

      // Update status
      await updateInstanceStatus(instanceId, 'deprovisioning');

      // Start async deprovisioning
      this.deprovisionAsync(instanceId, instance.dokku_app_name)
        .catch(error => {
          logger.error(`Async deprovisioning failed for ${instanceId}:`, error);
        });

      return {
        success: true,
        message: 'Deprovisioning started'
      };

    } catch (error) {
      logger.error('Deprovisioning failed:', error);
      throw error;
    }
  }

  async deprovisionAsync(instanceId, appName) {
    try {
      await dokku.connect();

      logger.info(`Destroying Dokku apps for ${appName}...`);

      // Destroy all apps
      const apps = [
        appName,
        `${appName}-backend`,
        `${appName}-frontend`,
        `${appName}-matrix`
      ];

      for (const app of apps) {
        if (await dokku.appExists(app)) {
          await dokku.destroyApp(app);
        }
      }

      // Delete instance record
      const { error } = await supabase
        .from('instances')
        .delete()
        .eq('id', instanceId);

      if (error) {
        logger.error('Failed to delete instance record:', error);
      }

      logger.info(`✅ Instance ${instanceId} deprovisioned successfully`);

    } catch (error) {
      logger.error(`Failed to deprovision instance ${instanceId}:`, error);
      throw error;
    } finally {
      await dokku.disconnect();
    }
  }
}

export const provisioner = new ProvisionerService();
