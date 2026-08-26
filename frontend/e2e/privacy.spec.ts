/**
 * The privacy page in a real browser, signed out: reachable by URL and still
 * there after a reload -- the two things the SPA rewrite and the service
 * worker's navigate fallback have to get right for a page nobody links to
 * from the home screen.
 */
import { expect, test } from '@playwright/test'

test('/privacy opens signed out and survives a reload', async ({ page }) => {
  await page.goto('/privacy')
  await expect(page.getByRole('heading', { name: 'What Drift keeps' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'The companion' })).toBeVisible()

  await page.reload()

  await expect(page.getByRole('heading', { name: 'What Drift keeps' })).toBeVisible()
  await expect(page).toHaveURL(/\/privacy$/)
})
