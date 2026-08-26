import logging
import time
from selenium import webdriver
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

#: Chrome flags for an unattended headless run.
_CHROME_ARGS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1920,1080",
)

#: Tried in order; the first visible, enabled match wins. Ordered most to least
#: specific, so a page carrying both an email field and a stray text input
#: picks the email field rather than whichever the DOM happens to list first.
_USER_FIELD_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[name*='user']",
    "input[type='text']",
    "#username",
    "#email",
)

#: Phrases meaning the site rejected the credentials. Matched against rendered
#: body text, because these forms rarely change status code or URL.
_LOGIN_ERROR_PHRASES = (
    "invalid password", "incorrect password", "invalid username",
    "wrong credentials", "login failed", "incorrect email",
    "bad credentials", "check your password",
)

#: Keys that are never an auth token on their own, however common.
_IGNORED_KEYS = frozenset({
    'id', 'uuid', 'uid', 'session', 'user', 'lang', 'preference', 'theme',
})

#: Substrings marking a key as telemetry unless it also says token/auth.
_WEAK_SUBSTRINGS = ('device', 'track', 'analytic', 'pixel', 'ga', 'aws', 'optimizely')

#: Substrings marking a key as worth keeping.
_STRONG_SIGNALS = (
    'access_token', 'refresh_token', 'id_token', 'auth', 'bearer', 'jwt',
    'session_id', 'sessionid', 'token',
)


def looks_like_auth_token(key: str) -> bool:
    """Whether a storage/cookie key is worth capturing as an auth token.

    Keyed on the name alone -- the value is not inspected, which is why this
    takes no value argument. The nested version this replaced declared one and
    never read it.

    Conservative in both directions on purpose: a false positive files
    someone's analytics id in a credential vault, while a false negative only
    means the connector asks them to sign in again.
    """
    k = key.lower()
    if k in _IGNORED_KEYS:
        return False
    for weak in _WEAK_SUBSTRINGS:
        if weak in k and 'token' not in k and 'auth' not in k:
            return False
    return any(signal in k for signal in _STRONG_SIGNALS)


def login_error_in(page_text: str):
    """The first rejection phrase present in the page text, or None."""
    low = page_text.lower()
    return next((phrase for phrase in _LOGIN_ERROR_PHRASES if phrase in low), None)


def _build_driver():
    options = Options()
    for arg in _CHROME_ARGS:
        options.add_argument(arg)
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=options,
    )
    # Keep an unresponsive page from hanging the worker indefinitely.
    driver.set_page_load_timeout(15)
    driver.set_script_timeout(15)
    return driver


def _find_first_interactable(driver, selectors):
    """First visible, enabled element matching any selector, in order."""
    for selector in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed() and el.is_enabled():
                    return el
        except WebDriverException:
            # This selector did not match on this page; try the next one.
            continue
    return None


def _collect_tokens(driver) -> dict:
    """Auth-looking values from localStorage, sessionStorage and cookies.

    Prefixed by origin, so two stores holding the same key stay distinct.
    """
    tokens = {}
    for prefix, store in (
        ("ls", driver.execute_script("return window.localStorage;")),
        ("ss", driver.execute_script("return window.sessionStorage;")),
    ):
        for k, v in (store or {}).items():
            if looks_like_auth_token(k):
                tokens[f"{prefix}_{k}"] = v
    for cookie in driver.get_cookies():
        if looks_like_auth_token(cookie['name']):
            tokens[f"cookie_{cookie['name']}"] = cookie['value']
    return tokens


def login_and_extract_tokens(url, username, password):
    """Drive a login form with Selenium and harvest the resulting auth tokens.

    Args:
        url (str): Login page URL
        username (str): Username/Email
        password (str): Password

    Returns:
        dict: Extracted tokens keyed by origin (`ls_` / `ss_` / `cookie_`).
    """
    logger.info(f"Starting browser automation for {url}")
    try:
        driver = _build_driver()
        try:
            driver.get(url)
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script('return document.readyState') == 'complete'
            )

            user_input = _find_first_interactable(driver, _USER_FIELD_SELECTORS)
            if not user_input:
                logger.error("Could not find a username input field")
                raise Exception("Could not find username field")
            user_input.clear()
            user_input.send_keys(username)

            try:
                pass_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            except NoSuchElementException:
                raise Exception("Could not find password field")
            pass_input.clear()
            pass_input.send_keys(password)

            try:
                driver.find_element(
                    By.CSS_SELECTOR, "button[type='submit'], input[type='submit']"
                ).click()
            except (NoSuchElementException, ElementNotInteractableException):
                # No usable submit button -- submit the form from the password field.
                pass_input.submit()

            # There is no known success URL to wait for, so the only option is
            # to let the navigation settle.
            logger.info("Credentials submitted, waiting for navigation...")
            time.sleep(5)

            failure = login_error_in(driver.find_element(By.TAG_NAME, "body").text)
            if failure:
                logger.warning(f"Login failure detected via text: {failure}")
                raise Exception(f"Login failed: Site said '{failure}'")

            tokens = _collect_tokens(driver)
            if not tokens:
                logger.warning("No potential auth tokens found in storage after login attempt")
            return tokens
        finally:
            driver.quit()
    except Exception as e:
        logger.error(f"Browser automation failed: {e}")
        raise e
