import { Page, Locator } from '@playwright/test';

export class ConfigDialog {
  readonly page: Page;
  readonly dialog: Locator;
  readonly saveButton: Locator;
  readonly cancelButton: Locator;

  constructor(page: Page) {
    this.page = page;
    this.dialog = page.getByRole('dialog');
    this.saveButton = this.dialog.getByRole('button', { name: /Save Configuration/i });
    this.cancelButton = this.dialog.getByRole('button', { name: /Cancel/i });
  }

  async waitForOpen() {
    await this.dialog.waitFor({ state: 'visible', timeout: 5000 });
  }

  async fillField(labelText: string, value: string) {
    const field = this.dialog.getByLabel(labelText);
    await field.fill(value);
  }

  async fillTelegramConfig(token: string) {
    await this.fillField('Bot Token', token);
  }

  async fillEmailConfig(config: {
    host: string;
    port: string;
    username: string;
    password: string;
  }) {
    await this.fillField('SMTP Host', config.host);
    await this.fillField('SMTP Port', config.port);
    await this.fillField('Username', config.username);
    await this.fillField('Password', config.password);
  }

  async save() {
    await this.saveButton.click();
  }

  async cancel() {
    await this.cancelButton.click();
  }

  async isOpen() {
    return this.dialog.isVisible();
  }

  async waitForClose() {
    await this.dialog.waitFor({ state: 'hidden', timeout: 5000 });
  }
}
