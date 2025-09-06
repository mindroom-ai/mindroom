import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import rateLimit from 'express-rate-limit';
import dotenv from 'dotenv';
import { logger } from './utils/logger.js';
import { provisionRouter } from './routes/provision.js';
import { healthRouter } from './routes/health.js';
import { instanceRouter } from './routes/instance.js';

// Load environment variables
dotenv.config({ path: '../../../.env' });

const app = express();
const PORT = process.env.DOKKU_PROVISIONER_PORT || 8002;

// Security middleware
app.use(helmet());
app.use(cors({
  origin: [
    process.env.NEXT_PUBLIC_APP_URL,
    'http://localhost:3001',
    'http://localhost:3002',
  ],
  credentials: true
}));

// Rate limiting
const limiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 100, // Limit each IP to 100 requests per windowMs
  message: 'Too many requests from this IP, please try again later.'
});
app.use('/api/', limiter);

// Body parsing
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Request logging
app.use((req, res, next) => {
  logger.info(`${req.method} ${req.path}`, {
    ip: req.ip,
    userAgent: req.get('user-agent')
  });
  next();
});

// Routes
app.use('/health', healthRouter);
app.use('/api/v1/provision', provisionRouter);
app.use('/api/v1/instance', instanceRouter);

// Error handling
app.use((err, req, res, next) => {
  logger.error('Unhandled error:', err);
  res.status(500).json({
    error: 'Internal server error',
    message: process.env.NODE_ENV === 'development' ? err.message : undefined
  });
});

// 404 handler
app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

// Start server
app.listen(PORT, () => {
  logger.info(`🚀 Dokku Provisioner running on port ${PORT}`);
  logger.info(`Environment: ${process.env.NODE_ENV || 'development'}`);
  logger.info(`Dokku Host: ${process.env.DOKKU_HOST || 'not configured'}`);
});
