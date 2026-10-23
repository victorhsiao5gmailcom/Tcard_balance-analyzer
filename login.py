from pathlib import Path
from playwright.sync_api import sync_playwright

TARGET_URL = "https://eacct-utsc-sp.transactcampus.com/eAccounts/AccountTransaction.aspx"
STATE_FILE = Path("auth_state.json")   # saved cookies/session
PROFILE_DIR = Path("chromium_profile") # persistent browser profile


def login_and_save_session():
    with sync_playwright() as p:
        # Persistent context keeps cookies and local storage between runs
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,  # visible window
            viewport={"width": 1200, "height": 850},
        )
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(TARGET_URL)

        print("A Chromium window has opened.")
        print("Please log in and complete the Duo verification there.")

        # Wait until we are back on the eAccounts transaction page after login.
        # The login goes through UofT SSO / Duo on a different domain, so we
        # wait for the URL to return to the transactcampus domain and the page
        # to show it is no longer a login page.
        page.wait_for_url(
            lambda url: "transactcampus.com/eAccounts/AccountTransaction" in url,
            timeout=0,  # no timeout: wait as long as the user needs
        )
        page.wait_for_load_state("networkidle")

        print("Login detected. Current URL:", page.url)

        # Save session state so later steps can reuse it
        context.storage_state(path=str(STATE_FILE))
        print(f"Session saved to {STATE_FILE.resolve()}")

        input("Press Enter to close the browser...")
        context.close()


if __name__ == "__main__":
    login_and_save_session()