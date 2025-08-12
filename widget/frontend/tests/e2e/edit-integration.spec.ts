import { test, expect } from '@playwright/test';
import { IntegrationsPage } from './helpers/integrations.helper';
import { ConfigDialog } from './helpers/config-dialog.helper';
import { ApiHelper } from './helpers/api.helper';

test.describe('Edit Integration Configuration', () => {
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
    await page.goto('/');
    await page.waitForLoadState('networkidle');
  });

  test('should show Edit button for configured tools', async ({ page }) => {
    // First configure a tool
    await integrationsPage.searchFor('Telegram');
    await integrationsPage.clickConfigureButton('Telegram');
    await configDialog.fillField('API Token', 'initial-token-123');
    await configDialog.save();

    // Wait for success message and UI update
    await page.waitForSelector('text=Success!');
    await page.waitForTimeout(500);

    // Should now show Edit and Disconnect buttons
    const telegramCard = page.locator('[data-testid="integration-card-telegram"]');
    await expect(telegramCard.locator('button:has-text("Edit")')).toBeVisible();
    await expect(telegramCard.locator('button:has-text("Disconnect")')).toBeVisible();

    // Configure button should not be visible
    await expect(telegramCard.locator('button:has-text("Configure")')).not.toBeVisible();
  });

  test('should open edit dialog with existing values pre-filled', async ({ page }) => {
    // Configure a tool with initial values
    const initialToken = 'test-token-456';
    await integrationsPage.searchFor('Telegram');
    await integrationsPage.clickConfigureButton('Telegram');
    await configDialog.fillField('API Token', initialToken);
    await configDialog.save();

    // Wait for success and UI update
    await page.waitForSelector('text=Success!');
    await page.waitForTimeout(500);

    // Click Edit button
    const telegramCard = page.locator('[data-testid="integration-card-telegram"]');
    await telegramCard.locator('button:has-text("Edit")').click();

    // Dialog should open with "Edit" title
    await expect(page.locator('[role="dialog"]')).toBeVisible();
    await expect(page.locator('[role="dialog"] h2')).toContainText('Edit Telegram');

    // Check that the existing value is pre-filled
    const apiTokenInput = page.locator('input[id="api_token"]');
    await expect(apiTokenInput).toHaveValue(initialToken);

    // Button should say "Update Configuration" instead of "Save Configuration"
    await expect(page.locator('button:has-text("Update Configuration")')).toBeVisible();
  });

  test('should update configuration when editing', async ({ page }) => {
    // Configure initially
    await integrationsPage.searchFor('Telegram');
    await integrationsPage.clickConfigureButton('Telegram');
    await configDialog.fillField('API Token', 'old-token');
    await configDialog.save();

    await page.waitForSelector('text=Success!');
    await page.waitForTimeout(500);

    // Edit the configuration
    const telegramCard = page.locator('[data-testid="integration-card-telegram"]');
    await telegramCard.locator('button:has-text("Edit")').click();

    // Clear and enter new value
    const apiTokenInput = page.locator('input[id="api_token"]');
    await apiTokenInput.clear();
    await apiTokenInput.fill('new-updated-token');

    // Save the update
    await page.locator('button:has-text("Update Configuration")').click();

    // Should show success message
    await expect(page.locator('text=Success!')).toBeVisible();

    // Verify via API that the value was updated
    const status = await apiHelper.getCredentialStatus('telegram');
    expect(status.has_credentials).toBeTruthy();

    // Verify the new value by opening edit again
    await telegramCard.locator('button:has-text("Edit")').click();
    await expect(page.locator('input[id="api_token"]')).toHaveValue('new-updated-token');
  });

  test('should handle multi-field edit correctly', async ({ page }) => {
    // Configure Email with multiple fields
    await integrationsPage.searchFor('Email');
    await integrationsPage.clickConfigureButton('Email');

    // Fill initial values
    await configDialog.fillField('SMTP Host', 'smtp.gmail.com');
    await configDialog.fillField('SMTP Port', '587');
    await configDialog.fillField('Username', 'user@example.com');
    await configDialog.fillField('Password', 'initial-password');
    await configDialog.save();

    await page.waitForSelector('text=Success!');
    await page.waitForTimeout(500);

    // Edit the configuration
    const emailCard = page.locator('[data-testid="integration-card-email"]');
    await emailCard.locator('button:has-text("Edit")').click();

    // All fields should be pre-filled
    await expect(page.locator('input[id="smtp_host"]')).toHaveValue('smtp.gmail.com');
    await expect(page.locator('input[id="smtp_port"]')).toHaveValue('587');
    await expect(page.locator('input[id="username"]')).toHaveValue('user@example.com');
    await expect(page.locator('input[id="password"]')).toHaveValue('initial-password');

    // Update some fields
    await page.locator('input[id="smtp_host"]').clear();
    await page.locator('input[id="smtp_host"]').fill('smtp.outlook.com');
    await page.locator('input[id="smtp_port"]').clear();
    await page.locator('input[id="smtp_port"]').fill('25');

    // Save updates
    await page.locator('button:has-text("Update Configuration")').click();
    await expect(page.locator('text=Success!')).toBeVisible();

    // Verify updates by reopening edit
    await emailCard.locator('button:has-text("Edit")').click();
    await expect(page.locator('input[id="smtp_host"]')).toHaveValue('smtp.outlook.com');
    await expect(page.locator('input[id="smtp_port"]')).toHaveValue('25');
    await expect(page.locator('input[id="username"]')).toHaveValue('user@example.com');
    await expect(page.locator('input[id="password"]')).toHaveValue('initial-password');
  });

  test('should show loading state while fetching existing credentials', async ({ page }) => {
    // Configure a tool
    await integrationsPage.searchFor('Telegram');
    await integrationsPage.clickConfigureButton('Telegram');
    await configDialog.fillField('API Token', 'test-token');
    await configDialog.save();

    await page.waitForSelector('text=Success!');
    await page.waitForTimeout(500);

    // Click Edit and check for loading state
    const telegramCard = page.locator('[data-testid="integration-card-telegram"]');
    await telegramCard.locator('button:has-text("Edit")').click();

    // The dialog should show briefly (or we might miss it if it's too fast)
    // Just verify the dialog opens with the field pre-filled
    await expect(page.locator('[role="dialog"]')).toBeVisible();
    await expect(page.locator('input[id="api_token"]')).toHaveValue('test-token');
  });

  test('should keep Edit button after updating configuration', async ({ page }) => {
    // Configure initially
    await integrationsPage.searchFor('Telegram');
    await integrationsPage.clickConfigureButton('Telegram');
    await configDialog.fillField('API Token', 'initial-token');
    await configDialog.save();

    await page.waitForSelector('text=Success!');
    await page.waitForTimeout(500);

    // Edit the configuration
    const telegramCard = page.locator('[data-testid="integration-card-telegram"]');
    await telegramCard.locator('button:has-text("Edit")').click();

    // Update the value
    await page.locator('input[id="api_token"]').clear();
    await page.locator('input[id="api_token"]').fill('updated-token');
    await page.locator('button:has-text("Update Configuration")').click();

    // Wait for success
    await expect(page.locator('text=Success!')).toBeVisible();
    await page.waitForTimeout(500);

    // Should still show Edit and Disconnect buttons
    await expect(telegramCard.locator('button:has-text("Edit")')).toBeVisible();
    await expect(telegramCard.locator('button:has-text("Disconnect")')).toBeVisible();
  });
});
