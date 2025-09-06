import { test, expect } from '@playwright/test';

const BASE_URL = process.env.ADMIN_URL || 'http://localhost:3001';
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || 'admin@mindroom.test';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'AdminPass123!';

test.describe('Admin Dashboard E2E Tests', () => {
  test('should load the login page', async ({ page }) => {
    await page.goto(BASE_URL);

    // Check for login form elements
    await expect(page.locator('input[name="username"], input[type="email"]')).toBeVisible();
    await expect(page.locator('input[name="password"], input[type="password"]')).toBeVisible();
    await expect(page.getByRole('button', { name: /Sign In|Login/i })).toBeVisible();
  });

  test('should show error with invalid credentials', async ({ page }) => {
    await page.goto(BASE_URL);

    // Enter invalid credentials
    await page.fill('input[name="username"], input[type="email"]', 'invalid@test.com');
    await page.fill('input[name="password"], input[type="password"]', 'wrongpassword');

    // Submit form
    await page.getByRole('button', { name: /Sign In|Login/i }).click();

    // Should show error message
    await expect(page.getByText(/invalid|error|failed/i)).toBeVisible({ timeout: 10000 });
  });

  test('should login with valid credentials', async ({ page }) => {
    await page.goto(BASE_URL);

    // Enter valid credentials
    await page.fill('input[name="username"], input[type="email"]', ADMIN_EMAIL);
    await page.fill('input[name="password"], input[type="password"]', ADMIN_PASSWORD);

    // Submit form
    await page.getByRole('button', { name: /Sign In|Login/i }).click();

    // Should redirect to dashboard
    await page.waitForURL((url) => !url.href.includes('login'), { timeout: 10000 });

    // Should show dashboard elements
    const dashboardLoaded =
      await page.locator('.MuiDrawer-root').count() > 0 ||
      await page.getByRole('navigation').count() > 0 ||
      await page.getByText(/Dashboard|Accounts|Subscriptions/i).count() > 0;

    expect(dashboardLoaded).toBeTruthy();
  });

  test('should navigate to main sections when logged in', async ({ page }) => {
    // Login first
    await page.goto(BASE_URL);
    await page.fill('input[name="username"], input[type="email"]', ADMIN_EMAIL);
    await page.fill('input[name="password"], input[type="password"]', ADMIN_PASSWORD);
    await page.getByRole('button', { name: /Sign In|Login/i }).click();

    // Wait for dashboard to load
    await page.waitForTimeout(2000);

    // Check for main navigation items
    const navigationItems = [
      'Accounts',
      'Subscriptions',
      'Instances',
      'Audit'
    ];

    for (const item of navigationItems) {
      const element = page.getByText(item);
      if (await element.count() > 0) {
        expect(await element.isVisible()).toBeTruthy();
      }
    }
  });

  test('should be able to logout', async ({ page }) => {
    // Login first
    await page.goto(BASE_URL);
    await page.fill('input[name="username"], input[type="email"]', ADMIN_EMAIL);
    await page.fill('input[name="password"], input[type="password"]', ADMIN_PASSWORD);
    await page.getByRole('button', { name: /Sign In|Login/i }).click();

    // Wait for dashboard to load
    await page.waitForTimeout(2000);

    // Find and click logout button
    const logoutButton = page.getByRole('button', { name: /Logout|Sign Out/i });
    if (await logoutButton.count() > 0) {
      await logoutButton.click();

      // Should redirect back to login
      await expect(page.locator('input[name="username"], input[type="email"]')).toBeVisible({ timeout: 10000 });
    }
  });
});

test.describe('Admin Dashboard API Tests', () => {
  test('should respond to health check', async ({ request }) => {
    const response = await request.get(`${BASE_URL}/api/health`);
    expect(response.ok()).toBeTruthy();
  });

  test('should have proper security headers', async ({ request }) => {
    const response = await request.get(BASE_URL);

    const headers = response.headers();
    expect(headers['x-frame-options']).toBe('SAMEORIGIN');
    expect(headers['x-xss-protection']).toBe('1; mode=block');
    expect(headers['x-content-type-options']).toBe('nosniff');
  });
});
