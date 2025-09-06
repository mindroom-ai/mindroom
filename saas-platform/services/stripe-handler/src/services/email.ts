import { Resend } from 'resend';
import { config } from '../config';
import { EmailData } from '../types';

// Initialize email client (Resend in this case, but can be swapped)
const resend = config.email.apiKey ? new Resend(config.email.apiKey) : null;

// Email templates
const templates = {
  welcome: {
    subject: 'Welcome to MindRoom! 🚀',
    html: (data: any) => `
      <!DOCTYPE html>
      <html>
      <head>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
          .container { max-width: 600px; margin: 0 auto; padding: 20px; }
          .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px 10px 0 0; }
          .content { background: white; padding: 30px; border: 1px solid #e5e5e5; border-radius: 0 0 10px 10px; }
          .button { display: inline-block; padding: 12px 24px; background: #667eea; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }
          .footer { text-align: center; color: #666; font-size: 12px; margin-top: 30px; }
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>Welcome to MindRoom!</h1>
          </div>
          <div class="content">
            <p>Hi there!</p>
            <p>Thank you for subscribing to MindRoom ${data.tier} tier. Your AI agents are ready to assist you across all your favorite chat platforms.</p>
            <p><strong>Your instance is being set up and will be available shortly at:</strong></p>
            <p><a href="${data.instanceUrl}" class="button">Access Your MindRoom</a></p>
            <h3>What's next?</h3>
            <ul>
              <li>Configure your AI agents in the dashboard</li>
              <li>Connect your chat platforms (Slack, Discord, Telegram, etc.)</li>
              <li>Start collaborating with your AI team</li>
            </ul>
            <p>If you have any questions, feel free to reach out to our support team.</p>
            <p>Best regards,<br>The MindRoom Team</p>
          </div>
          <div class="footer">
            <p>© 2024 MindRoom. Your AI, everywhere.</p>
          </div>
        </div>
      </body>
      </html>
    `,
  },

  trial_ending: {
    subject: 'Your MindRoom trial ends in 3 days',
    html: (data: any) => `
      <!DOCTYPE html>
      <html>
      <head>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
          .container { max-width: 600px; margin: 0 auto; padding: 20px; }
          .header { background: #fbbf24; color: #000; padding: 30px; border-radius: 10px 10px 0 0; }
          .content { background: white; padding: 30px; border: 1px solid #e5e5e5; border-radius: 0 0 10px 10px; }
          .button { display: inline-block; padding: 12px 24px; background: #667eea; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>Your Trial is Ending Soon</h1>
          </div>
          <div class="content">
            <p>Your MindRoom trial will end in 3 days on ${data.trialEndDate}.</p>
            <p>To continue using MindRoom and keep your AI agents running, please update your payment method:</p>
            <p><a href="${data.billingUrl}" class="button">Update Payment Method</a></p>
            <p>Don't lose your AI agents and all the context they've learned!</p>
          </div>
        </div>
      </body>
      </html>
    `,
  },

  payment_failed: {
    subject: '⚠️ Payment Failed - Action Required',
    html: (data: any) => `
      <!DOCTYPE html>
      <html>
      <head>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
          .container { max-width: 600px; margin: 0 auto; padding: 20px; }
          .header { background: #ef4444; color: white; padding: 30px; border-radius: 10px 10px 0 0; }
          .content { background: white; padding: 30px; border: 1px solid #e5e5e5; border-radius: 0 0 10px 10px; }
          .button { display: inline-block; padding: 12px 24px; background: #667eea; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }
          .warning { background: #fef2f2; border: 1px solid #fecaca; padding: 15px; border-radius: 5px; margin: 20px 0; }
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>Payment Failed</h1>
          </div>
          <div class="content">
            <p>We were unable to process your payment for your MindRoom subscription.</p>
            <div class="warning">
              <strong>⚠️ Important:</strong> Your service will be suspended in ${data.gracePeriodDays} days if payment is not received.
            </div>
            <p>Please update your payment method to continue using MindRoom:</p>
            <p><a href="${data.billingUrl}" class="button">Update Payment Method</a></p>
            <p>If you believe this is an error, please contact support immediately.</p>
          </div>
        </div>
      </body>
      </html>
    `,
  },

  subscription_cancelled: {
    subject: 'Your MindRoom subscription has been cancelled',
    html: (data: any) => `
      <!DOCTYPE html>
      <html>
      <head>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
          .container { max-width: 600px; margin: 0 auto; padding: 20px; }
          .header { background: #6b7280; color: white; padding: 30px; border-radius: 10px 10px 0 0; }
          .content { background: white; padding: 30px; border: 1px solid #e5e5e5; border-radius: 0 0 10px 10px; }
          .button { display: inline-block; padding: 12px 24px; background: #667eea; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>Subscription Cancelled</h1>
          </div>
          <div class="content">
            <p>Your MindRoom subscription has been cancelled.</p>
            <p>Your agents and data will remain available until ${data.accessEndDate}.</p>
            <p>After this date, your instance will be deprovisioned and data will be archived.</p>
            <p>We're sorry to see you go! If you change your mind, you can reactivate your subscription anytime:</p>
            <p><a href="${data.reactivateUrl}" class="button">Reactivate Subscription</a></p>
            <p>Thank you for being a MindRoom user.</p>
          </div>
        </div>
      </body>
      </html>
    `,
  },

  subscription_upgraded: {
    subject: '🎉 Your MindRoom subscription has been upgraded!',
    html: (data: any) => `
      <!DOCTYPE html>
      <html>
      <head>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
          .container { max-width: 600px; margin: 0 auto; padding: 20px; }
          .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px 10px 0 0; }
          .content { background: white; padding: 30px; border: 1px solid #e5e5e5; border-radius: 0 0 10px 10px; }
          .feature { background: #f3f4f6; padding: 15px; border-radius: 5px; margin: 10px 0; }
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>🎉 Upgrade Complete!</h1>
          </div>
          <div class="content">
            <p>Congratulations! Your MindRoom subscription has been upgraded to the <strong>${data.newTier}</strong> tier.</p>
            <h3>Your new limits:</h3>
            <div class="feature">
              <strong>Agents:</strong> ${data.agents === -1 ? 'Unlimited' : data.agents}<br>
              <strong>Messages/Day:</strong> ${data.messagesPerDay === -1 ? 'Unlimited' : data.messagesPerDay}<br>
              <strong>Memory:</strong> ${data.memoryMb} MB<br>
              <strong>CPU:</strong> ${data.cpuLimit} cores
            </div>
            <p>Your upgraded resources are available immediately. Enjoy your enhanced MindRoom experience!</p>
          </div>
        </div>
      </body>
      </html>
    `,
  },

  subscription_downgraded: {
    subject: 'Your MindRoom subscription has been downgraded',
    html: (data: any) => `
      <!DOCTYPE html>
      <html>
      <head>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
          .container { max-width: 600px; margin: 0 auto; padding: 20px; }
          .header { background: #fbbf24; color: #000; padding: 30px; border-radius: 10px 10px 0 0; }
          .content { background: white; padding: 30px; border: 1px solid #e5e5e5; border-radius: 0 0 10px 10px; }
          .warning { background: #fef2f2; border: 1px solid #fecaca; padding: 15px; border-radius: 5px; margin: 20px 0; }
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>Subscription Downgraded</h1>
          </div>
          <div class="content">
            <p>Your MindRoom subscription has been downgraded to the <strong>${data.newTier}</strong> tier.</p>
            <h3>Your new limits:</h3>
            <ul>
              <li>Agents: ${data.agents}</li>
              <li>Messages/Day: ${data.messagesPerDay}</li>
              <li>Memory: ${data.memoryMb} MB</li>
              <li>CPU: ${data.cpuLimit} cores</li>
            </ul>
            <div class="warning">
              <strong>Note:</strong> If you're currently using more resources than your new limits allow, some features may be restricted.
            </div>
          </div>
        </div>
      </body>
      </html>
    `,
  },
};

export async function sendEmail(data: EmailData): Promise<void> {
  if (!resend) {
    console.warn('Email service not configured, skipping email:', data.subject);
    return;
  }

  const template = templates[data.template];
  if (!template) {
    console.error(`Unknown email template: ${data.template}`);
    return;
  }

  try {
    await resend.emails.send({
      from: `${config.email.fromName} <${config.email.fromAddress}>`,
      to: data.to,
      subject: data.subject || template.subject,
      html: template.html(data.data),
    });

    console.log(`📧 Email sent to ${data.to}: ${data.template}`);
  } catch (error) {
    console.error('Failed to send email:', error);
    // Don't throw - email failure shouldn't break the webhook processing
  }
}

// Batch email sending for notifications
export async function sendBatchEmails(emails: EmailData[]): Promise<void> {
  for (const email of emails) {
    await sendEmail(email);
    // Add small delay to avoid rate limiting
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}
