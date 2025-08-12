import { Page, Locator } from '@playwright/test';

export class IntegrationsPage {
  readonly page: Page;
  readonly searchInput: Locator;
  readonly availableToggle: Locator;

  constructor(page: Page) {
    this.page = page;
    this.searchInput = page.getByPlaceholder('Search integrations...');
    this.availableToggle = page.getByRole('button', { name: /Available/i });
  }

  async goto() {
    await this.page.goto('/');
    // Wait for the page to load
    await this.page.waitForSelector('text=Integrations', { timeout: 10000 });
  }

  async searchFor(term: string) {
    await this.searchInput.fill(term);
  }

  async getIntegrationCard(name: string) {
    // Find the card containing the integration name
    return this.page.locator('.card').filter({ hasText: name }).first();
  }

  async getIntegrationStatus(name: string) {
    const card = await this.getIntegrationCard(name);
    const badge = card.locator('.badge').first();
    return badge.textContent();
  }

  async clickConfigureButton(integrationName: string) {
    const card = await this.getIntegrationCard(integrationName);
    await card.getByRole('button', { name: /Configure/i }).click();
  }

  async clickDisconnectButton(integrationName: string) {
    const card = await this.getIntegrationCard(integrationName);
    await card.getByRole('button', { name: /Disconnect/i }).click();
  }

  async isConfigureButtonVisible(integrationName: string) {
    const card = await this.getIntegrationCard(integrationName);
    const button = card.getByRole('button', { name: /Configure/i });
    return button.isVisible();
  }

  async isDisconnectButtonVisible(integrationName: string) {
    const card = await this.getIntegrationCard(integrationName);
    const button = card.getByRole('button', { name: /Disconnect/i });
    return button.isVisible();
  }

  async waitForToast(text: string) {
    // Wait for toast notification with specific text
    await this.page.locator(`text=${text}`).waitFor({ state: 'visible', timeout: 5000 });
  }
}
