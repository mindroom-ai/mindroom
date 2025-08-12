import { test, expect } from '@playwright/test';
import { IntegrationsPage } from './helpers/integrations.helper';
import { ConfigDialog } from './helpers/config-dialog.helper';
import { ApiHelper } from './helpers/api.helper';

test.describe('Tool Configuration Flow', () => {
  let integrationsPage: IntegrationsPage;
  let configDialog: ConfigDialog;
  let apiHelper: ApiHelper;

  test.beforeEach(async ({ page, request }) => {
    integrationsPage = new IntegrationsPage(page);
    configDialog = new ConfigDialog(page);
    apiHelper = new ApiHelper(request);

    // Clear all credentials before each test
    await apiHelper.clearAllCredentials();

    // Navigate to the integrations page
    await integrationsPage.goto();
  });

  test('should configure Telegram integration', async () => {
    // Search for Telegram
    await integrationsPage.searchFor('Telegram');

    // Check initial status
    const initialStatus = await integrationsPage.getIntegrationStatus('Telegram');
    expect(initialStatus).toContain('Available');

    // Check Configure button is visible
    expect(await integrationsPage.isConfigureButtonVisible('Telegram')).toBeTruthy();
    expect(await integrationsPage.isDisconnectButtonVisible('Telegram')).toBeFalsy();

    // Click Configure button
    await integrationsPage.clickConfigureButton('Telegram');

    // Wait for dialog to open
    await configDialog.waitForOpen();

    // Fill in the Telegram token
    await configDialog.fillTelegramConfig('123456:ABC-DEF-test-token');

    // Save configuration
    await configDialog.save();

    // Wait for success toast
    await integrationsPage.waitForToast('Telegram has been configured successfully');

    // Wait for dialog to close
    await configDialog.waitForClose();

    // Wait a moment for the UI to update
    await integrationsPage.page.waitForTimeout(1000);

    // Check that status is now Connected
    const newStatus = await integrationsPage.getIntegrationStatus('Telegram');
    expect(newStatus).toContain('Connected');

    // Check that Disconnect button is now visible
    expect(await integrationsPage.isDisconnectButtonVisible('Telegram')).toBeTruthy();
    expect(await integrationsPage.isConfigureButtonVisible('Telegram')).toBeFalsy();

    // Verify via API that credentials are saved
    const credStatus = await apiHelper.getCredentialStatus('telegram');
    expect(credStatus.has_credentials).toBeTruthy();
    expect(credStatus.key_names).toContain('TELEGRAM_TOKEN');
  });

  test('should disconnect Telegram integration', async () => {
    // First, set up credentials via API
    await apiHelper.setCredentials('telegram', {
      TELEGRAM_TOKEN: '123456:ABC-DEF-test-token',
    });

    // Reload page to get updated status
    await integrationsPage.goto();

    // Search for Telegram
    await integrationsPage.searchFor('Telegram');

    // Check that it shows as connected
    const initialStatus = await integrationsPage.getIntegrationStatus('Telegram');
    expect(initialStatus).toContain('Connected');

    // Click Disconnect button
    await integrationsPage.clickDisconnectButton('Telegram');

    // Wait for success toast
    await integrationsPage.waitForToast('Telegram has been disconnected');

    // Wait a moment for the UI to update
    await integrationsPage.page.waitForTimeout(1000);

    // Check that status is back to Available
    const newStatus = await integrationsPage.getIntegrationStatus('Telegram');
    expect(newStatus).toContain('Available');

    // Check that Configure button is visible again
    expect(await integrationsPage.isConfigureButtonVisible('Telegram')).toBeTruthy();
    expect(await integrationsPage.isDisconnectButtonVisible('Telegram')).toBeFalsy();

    // Verify via API that credentials are deleted
    const credStatus = await apiHelper.getCredentialStatus('telegram');
    expect(credStatus.has_credentials).toBeFalsy();
  });

  test('should configure Email integration with multiple fields', async () => {
    // Search for Email
    await integrationsPage.searchFor('Email');

    // Click Configure button
    await integrationsPage.clickConfigureButton('Email');

    // Wait for dialog to open
    await configDialog.waitForOpen();

    // Fill in the Email configuration
    await configDialog.fillEmailConfig({
      host: 'smtp.gmail.com',
      port: '587',
      username: 'test@example.com',
      password: 'test-password-123',
    });

    // Save configuration
    await configDialog.save();

    // Wait for success toast
    await integrationsPage.waitForToast('Email has been configured successfully');

    // Wait for dialog to close
    await configDialog.waitForClose();

    // Wait a moment for the UI to update
    await integrationsPage.page.waitForTimeout(1000);

    // Check that status is now Connected
    const newStatus = await integrationsPage.getIntegrationStatus('Email');
    expect(newStatus).toContain('Connected');

    // Verify via API that all credentials are saved
    const credStatus = await apiHelper.getCredentialStatus('email');
    expect(credStatus.has_credentials).toBeTruthy();
    expect(credStatus.key_names).toEqual(
      expect.arrayContaining(['SMTP_HOST', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD'])
    );
  });

  test('should show validation error for missing required fields', async () => {
    // Search for Email
    await integrationsPage.searchFor('Email');

    // Click Configure button
    await integrationsPage.clickConfigureButton('Email');

    // Wait for dialog to open
    await configDialog.waitForOpen();

    // Only fill in partial configuration
    await configDialog.fillField('SMTP Host', 'smtp.gmail.com');
    // Leave other required fields empty

    // Try to save configuration
    await configDialog.save();

    // Should show validation error toast
    await integrationsPage.waitForToast('Missing Configuration');

    // Dialog should still be open
    expect(await configDialog.isOpen()).toBeTruthy();

    // Cancel the dialog
    await configDialog.cancel();

    // Verify no credentials were saved
    const credStatus = await apiHelper.getCredentialStatus('email');
    expect(credStatus.has_credentials).toBeFalsy();
  });

  test('should filter integrations by availability', async () => {
    // Set up some credentials
    await apiHelper.setCredentials('telegram', {
      TELEGRAM_TOKEN: '123456:ABC-DEF-test-token',
    });

    // Reload page
    await integrationsPage.goto();

    // Count total integrations visible
    const allCards = await integrationsPage.page.locator('.card').count();
    expect(allCards).toBeGreaterThan(0);

    // Toggle "Show only available"
    await integrationsPage.availableToggle.click();

    // Count filtered integrations
    const filteredCards = await integrationsPage.page.locator('.card').count();
    expect(filteredCards).toBeLessThanOrEqual(allCards);

    // Telegram should be visible (it's connected)
    const telegramCard = await integrationsPage.getIntegrationCard('Telegram');
    expect(await telegramCard.isVisible()).toBeTruthy();
  });

  test('should persist configuration across page reloads', async () => {
    // Configure Telegram
    await integrationsPage.searchFor('Telegram');
    await integrationsPage.clickConfigureButton('Telegram');
    await configDialog.waitForOpen();
    await configDialog.fillTelegramConfig('123456:ABC-DEF-test-token');
    await configDialog.save();
    await integrationsPage.waitForToast('Telegram has been configured successfully');

    // Reload the page
    await integrationsPage.page.reload();
    await integrationsPage.page.waitForSelector('text=Integrations', { timeout: 10000 });

    // Search for Telegram again
    await integrationsPage.searchFor('Telegram');

    // Check that it's still connected
    const status = await integrationsPage.getIntegrationStatus('Telegram');
    expect(status).toContain('Connected');
    expect(await integrationsPage.isDisconnectButtonVisible('Telegram')).toBeTruthy();
  });
});
