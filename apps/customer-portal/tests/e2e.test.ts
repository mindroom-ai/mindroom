import { test, expect } from '@playwright/test';

const BASE_URL = process.env.BASE_URL || 'http://localhost:3002';

test.describe('Customer Portal E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
  });

  test('should load the homepage', async ({ page }) => {
    // Check that the page loads
    await expect(page).toHaveTitle(/MindRoom/);

    // Check for main elements
    await expect(page.getByRole('heading', { name: /Welcome/i })).toBeVisible();
  });

  test('should display login button when not authenticated', async ({ page }) => {
    // Look for login/sign in button
    const loginButton = page.getByRole('button', { name: /Sign In|Login/i });
    await expect(loginButton).toBeVisible();
  });

  test('should navigate to pricing page', async ({ page }) => {
    // Click on pricing link if it exists
    const pricingLink = page.getByRole('link', { name: /Pricing/i });
    if (await pricingLink.count() > 0) {
      await pricingLink.click();
      await expect(page).toHaveURL(/.*pricing.*/);
    }
  });

  test('should have responsive design', async ({ page }) => {
    // Test mobile viewport
    await page.setViewportSize({ width: 375, height: 667 });
    await expect(page.locator('body')).toBeVisible();

    // Test tablet viewport
    await page.setViewportSize({ width: 768, height: 1024 });
    await expect(page.locator('body')).toBeVisible();

    // Test desktop viewport
    await page.setViewportSize({ width: 1920, height: 1080 });
    await expect(page.locator('body')).toBeVisible();
  });

  test('should handle 404 pages gracefully', async ({ page }) => {
    await page.goto(`${BASE_URL}/non-existent-page`);

    // Should show 404 or redirect to home
    const is404 = await page.getByText(/404|not found/i).count() > 0;
    const isHome = await page.getByRole('heading', { name: /Welcome/i }).count() > 0;

    expect(is404 || isHome).toBeTruthy();
  });
});

test.describe('Customer Portal API Health Checks', () => {
  test('should respond to health check endpoint', async ({ request }) => {
    const response = await request.get(`${BASE_URL}/api/health`);
    expect(response.ok()).toBeTruthy();

    const data = await response.json();
    expect(data).toHaveProperty('status');
    expect(data.status).toBe('ok');
  });

  test('should have proper security headers', async ({ request }) => {
    const response = await request.get(BASE_URL);

    // Check for security headers
    const headers = response.headers();
    expect(headers['x-frame-options']).toBeDefined();
  });
});
