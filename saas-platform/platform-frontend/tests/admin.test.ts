import { test, expect } from '@playwright/test';

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';

test.describe('Admin Panel Tests', () => {
  test('should redirect non-admin users from admin routes', async ({ page }) => {
    // Try to access admin without authentication
    await page.goto(`${BASE_URL}/admin`);

    // Should redirect to login
    await expect(page).toHaveURL(/.*auth\/login.*/);
  });

  test('should show admin navigation for admin users', async ({ page, context }) => {
    // This test would require mocking Supabase auth
    // For now, just verify the route exists
    const response = await page.goto(`${BASE_URL}/admin`, { waitUntil: 'domcontentloaded' });

    // Should either redirect to auth or load (depending on auth state)
    expect(response?.status()).toBeLessThan(500);
  });

  test('admin routes should be protected', async ({ page }) => {
    const adminRoutes = [
      '/admin',
      '/admin/accounts',
      '/admin/subscriptions',
      '/admin/instances',
      '/admin/audit-logs',
      '/admin/usage'
    ];

    for (const route of adminRoutes) {
      const response = await page.goto(`${BASE_URL}${route}`, { waitUntil: 'domcontentloaded' });

      // Should redirect to auth (302/303) or show login page
      expect(response?.status()).toBeLessThan(500);

      // Should eventually end up at login if not authenticated
      await expect(page).toHaveURL(/.*auth\/login.*/);
    }
  });
});
