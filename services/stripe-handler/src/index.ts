import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import morgan from 'morgan';
import { webhookRouter } from './routes/webhooks';
import { config } from './config';

const app = express();

// Middleware
app.use(helmet());
app.use(cors());
app.use(morgan('combined'));

// IMPORTANT: Raw body for Stripe webhooks signature verification
app.use('/webhooks/stripe', express.raw({ type: 'application/json' }));
app.use(express.json());

// Routes
app.use('/webhooks', webhookRouter);

// Health check
app.get('/health', (_req, res) => {
  res.json({
    status: 'healthy',
    service: 'stripe-handler',
    timestamp: new Date().toISOString(),
    uptime: process.uptime()
  });
});

// Error handler
app.use((err: Error, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  console.error('Global error handler:', err);
  res.status(500).json({
    error: 'Internal server error',
    message: process.env.NODE_ENV === 'development' ? err.message : undefined
  });
});

// Start server
const server = app.listen(config.port, () => {
  console.log(`🚀 Stripe handler running on port ${config.port}`);
  console.log(`📊 Environment: ${process.env.NODE_ENV || 'development'}`);
  console.log(`🔐 Webhook endpoint: http://localhost:${config.port}/webhooks/stripe`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
  console.log('SIGTERM signal received: closing HTTP server');
  server.close(() => {
    console.log('HTTP server closed');
    process.exit(0);
  });
});

export default app;
