"""Check that a fresh browser editor opens its first FrostPi chat automatically."""
import argparse
import time
from playwright.sync_api import sync_playwright


def check(url, screenshot, cdp=None):
    with sync_playwright() as playwright:
        browser = (playwright.chromium.connect_over_cdp(cdp) if cdp
                   else playwright.chromium.launch(headless=True))
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.goto(url)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                for frame in page.frames:
                    composer = frame.get_by_label('Message Pi', exact=True)
                    if composer.count() and composer.first.is_visible():
                        composer.first.fill('Browser keyboard fixture')
                        assert composer.first.inner_text() == 'Browser keyboard fixture'
                        composer.first.fill('')
                        page.screenshot(path=screenshot)
                        print('PASS FrostPi composer loaded without clicking its tab; typing works')
                        return
                page.wait_for_timeout(250)
            page.screenshot(path=screenshot)
            raise AssertionError('FrostPi composer did not appear within 60 seconds')
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='URL of a fresh disposable browser editor, including its folder query')
    parser.add_argument('--screenshot', required=True)
    parser.add_argument('--cdp', help='Existing Chromium/Edge debugging URL, for testing the host browser from WSL')
    args = parser.parse_args()
    check(args.url, args.screenshot, args.cdp)
