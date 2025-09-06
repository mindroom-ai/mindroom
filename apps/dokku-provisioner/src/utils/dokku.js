import { NodeSSH } from 'node-ssh';
import { logger } from './logger.js';

class DokkuClient {
  constructor() {
    this.ssh = new NodeSSH();
    this.connected = false;
  }

  async connect() {
    if (this.connected) return;

    const config = {
      host: process.env.DOKKU_HOST,
      username: process.env.DOKKU_USER || 'dokku-deploy',  // Use dokku-deploy user from terraform
      port: parseInt(process.env.DOKKU_PORT || '22'),
    };

    // Use SSH key if available, otherwise use password
    if (process.env.DOKKU_SSH_KEY_PATH) {
      const fs = await import('fs');
      config.privateKey = fs.readFileSync(process.env.DOKKU_SSH_KEY_PATH, 'utf8');
    } else if (process.env.DOKKU_SSH_KEY) {
      config.privateKey = process.env.DOKKU_SSH_KEY;
    } else if (process.env.DOKKU_PASSWORD) {
      config.password = process.env.DOKKU_PASSWORD;
    } else {
      throw new Error('No SSH authentication method configured for Dokku');
    }

    try {
      await this.ssh.connect(config);
      this.connected = true;
      logger.info('Connected to Dokku server');
    } catch (error) {
      logger.error('Failed to connect to Dokku server:', error);
      throw error;
    }
  }

  async disconnect() {
    if (this.connected) {
      this.ssh.dispose();
      this.connected = false;
    }
  }

  async execute(command) {
    if (!this.connected) {
      await this.connect();
    }

    try {
      // Prepend sudo dokku if not already present and we're using dokku-deploy user
      const user = process.env.DOKKU_USER || 'dokku-deploy';
      const finalCommand = (user === 'dokku-deploy' && !command.startsWith('sudo'))
        ? `sudo dokku ${command.replace(/^dokku\s+/, '')}`
        : command;

      logger.debug(`Executing: ${finalCommand}`);
      const result = await this.ssh.execCommand(finalCommand);

      if (result.stderr && result.code !== 0) {
        throw new Error(`Command failed: ${result.stderr}`);
      }

      return result.stdout;
    } catch (error) {
      logger.error(`Command execution failed: ${command}`, error);
      throw error;
    }
  }

  // Dokku app management
  async createApp(appName) {
    await this.execute(`dokku apps:create ${appName}`);
    logger.info(`Created Dokku app: ${appName}`);
  }

  async destroyApp(appName) {
    await this.execute(`dokku apps:destroy ${appName} --force`);
    logger.info(`Destroyed Dokku app: ${appName}`);
  }

  async appExists(appName) {
    try {
      const apps = await this.execute('dokku apps:list');
      return apps.includes(appName);
    } catch {
      return false;
    }
  }

  // Domain management
  async addDomain(appName, domain) {
    await this.execute(`dokku domains:add ${appName} ${domain}`);
    logger.info(`Added domain ${domain} to ${appName}`);
  }

  async enableLetsencrypt(appName) {
    await this.execute(`dokku letsencrypt:enable ${appName}`);
    logger.info(`Enabled Let's Encrypt for ${appName}`);
  }

  // Environment variables
  async setConfig(appName, config) {
    const configString = Object.entries(config)
      .map(([key, value]) => `${key}="${value}"`)
      .join(' ');

    await this.execute(`dokku config:set --no-restart ${appName} ${configString}`);
    logger.info(`Set config for ${appName}`);
  }

  // Docker deployment
  async deployDockerImage(appName, imageName) {
    // Tag and deploy the Docker image
    await this.execute(`docker pull ${imageName}`);
    await this.execute(`docker tag ${imageName} dokku/${appName}:latest`);
    await this.execute(`dokku deploy ${appName} latest`);
    logger.info(`Deployed ${imageName} to ${appName}`);
  }

  // Database management
  async createPostgres(dbName) {
    await this.execute(`dokku postgres:create ${dbName}`);
    logger.info(`Created Postgres database: ${dbName}`);
  }

  async linkPostgres(appName, dbName) {
    await this.execute(`dokku postgres:link ${dbName} ${appName}`);
    logger.info(`Linked Postgres ${dbName} to ${appName}`);
  }

  async createRedis(redisName) {
    await this.execute(`dokku redis:create ${redisName}`);
    logger.info(`Created Redis instance: ${redisName}`);
  }

  async linkRedis(appName, redisName) {
    await this.execute(`dokku redis:link ${redisName} ${appName}`);
    logger.info(`Linked Redis ${redisName} to ${appName}`);
  }

  // Resource limits
  async setResourceLimits(appName, limits) {
    if (limits.memory) {
      await this.execute(`dokku resource:limit ${appName} --memory ${limits.memory}`);
    }
    if (limits.cpu) {
      await this.execute(`dokku resource:limit ${appName} --cpu ${limits.cpu}`);
    }
    logger.info(`Set resource limits for ${appName}`);
  }

  // App management
  async stopApp(appName) {
    await this.execute(`dokku ps:stop ${appName}`);
    logger.info(`Stopped app: ${appName}`);
  }

  async startApp(appName) {
    await this.execute(`dokku ps:start ${appName}`);
    logger.info(`Started app: ${appName}`);
  }

  async restartApp(appName) {
    await this.execute(`dokku ps:restart ${appName}`);
    logger.info(`Restarted app: ${appName}`);
  }

  async getAppUrl(appName) {
    const urls = await this.execute(`dokku urls ${appName}`);
    return urls.split('\n')[0]; // Return first URL
  }
}

export const dokku = new DokkuClient();
