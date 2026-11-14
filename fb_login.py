"""
One-time Facebook login for deep scans.

Logged-out Facebook Marketplace returns at most 24 results per search and then shows a login dialog,
so a deep scan can only see "everything" through a logged-in session. This opens a real Chrome window
using a private profile folder (./fb_browser_profile). Log in, then press Enter here. Scans reuse it.

Use a throwaway Facebook account, not your main one: automated scraping violates Facebook's terms
and can get the account restricted.

  .venv/bin/python fb_login.py
"""
import asyncio

from playwright.async_api import async_playwright

from scrapers import PROFILE_DIR, _launch_profile_context


async def main() -> None:
    async with async_playwright() as p:
        ctx = await _launch_profile_context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.facebook.com/marketplace/")
        print("Log in to Facebook in the browser window (use a throwaway account).")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter here once you're logged in… ")
        await ctx.close()
    print(f"Saved session to {PROFILE_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
