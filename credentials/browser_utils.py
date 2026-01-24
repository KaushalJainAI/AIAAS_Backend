import logging
import time
import json
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

def login_and_extract_tokens(url, username, password):
    """
    Automates login flow using Selenium and extracts tokens.
    
    Args:
        url (str): Login page URL
        username (str): Username/Email
        password (str): Password
        
    Returns:
        dict: Extracted tokens (access_token, refresh_token) or other relevant data found in storage.
    """
    logger.info(f"Starting browser automation for {url}")
    
    options = Options()
    options.add_argument("--headless=new") # Run in headless mode
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(15) # Prevent infinite hanging
        driver.set_script_timeout(15)
        
        try:
            # 1. Navigate to Login Page
            driver.get(url)
            
            # 2. Wait for page load (rudimentary check)
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script('return document.readyState') == 'complete'
            )
            
            # 3. Find and Fill Username
            # Heuristic: Find first visible input of type text/email or name containing 'user'/'email'
            user_input = None
            possible_user_selectors = [
                "input[type='email']",
                "input[name='email']",
                "input[name*='user']",
                "input[type='text']",
                "#username",
                "#email"
            ]
            
            for selector in possible_user_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for el in elements:
                        if el.is_displayed() and el.is_enabled():
                            user_input = el
                            break
                    if user_input:
                        break
                except:
                    continue
            
            if not user_input:
                logger.error("Could not specific username input field")
                # Fallback: try finding by label? Too complex for generic script v1.
                raise Exception("Could not find username field")
                
            user_input.clear()
            user_input.send_keys(username)
            
            # 4. Find and Fill Password
            pass_input = None
            try:
                pass_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            except:
                pass 
                
            if not pass_input:
                 raise Exception("Could not find password field")
                 
            pass_input.clear()
            pass_input.send_keys(password)
            
            # 5. Submit
            # Try finding a submit button or submit form
            try:
                submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
                submit_btn.click()
            except:
                # Fallback: hit enter on password field
                pass_input.submit()
                
            # 6. Wait for Login to Complete
            # Heuristics: URL change, or specific token appearance in storage
            logger.info("Credentials submitted, waiting for navigation...")
            
            # Wait up to 10 seconds for URL to change OR storage to have items
            # This is tricky because we don't know the success URL.
            # Let's wait a bit for network idle
            time.sleep(5) 
            
            # 7. Check for Login Error Messages on Page
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            error_keywords = [
                "invalid password", "incorrect password", "invalid username", 
                "wrong credentials", "login failed", "incorrect email", 
                "bad credentials", "check your password"
            ]
            
            for err in error_keywords:
                if err in page_text:
                    logger.warning(f"Login failure detected via text: {err}")
                    raise Exception(f"Login failed: Site said '{err}'")

            # 8. Extract Tokens (STRICTER)
            tokens = {}
            
            # Helper to check if a key looks like a strong auth token
            def is_strong_token(key, value):
                k = key.lower()
                # Ignore generic 'id', 'session', 'user', 'tracking' unless combined with 'token'
                if k in ['id', 'uuid', 'uid', 'session', 'user', 'lang', 'preference', 'theme']:
                     return False
                     
                # Weak keywords that need to be skipped if they are standalone
                weak_substrings = ['device', 'track', 'analytic', 'pixel', 'ga', 'aws', 'optimizely']
                for weak in weak_substrings:
                    if weak in k and 'token' not in k and 'auth' not in k:
                        return False
                
                # Strong keywords
                strong_signals = ['access_token', 'refresh_token', 'id_token', 'auth', 'bearer', 'jwt', 'session_id', 'sessionid', 'token']
                
                for signal in strong_signals:
                    if signal in k:
                        return True
                return False

            # Local Storage
            local_storage = driver.execute_script("return window.localStorage;")
            if local_storage:
                for k, v in local_storage.items():
                    if is_strong_token(k, v):
                        tokens[f"ls_{k}"] = v
                        
            # Session Storage
            session_storage = driver.execute_script("return window.sessionStorage;")
            if session_storage:
                for k, v in session_storage.items():
                    if is_strong_token(k, v):
                         tokens[f"ss_{k}"] = v
                        
            # Cookies
            cookies = driver.get_cookies()
            for cookie in cookies:
                name = cookie['name']
                if is_strong_token(name, cookie['value']):
                    tokens[f"cookie_{name}"] = cookie['value']
                    
            if not tokens:
                # Maybe it was a failed login?
                logger.warning("No potential auth tokens found in storage after login attempt")
                
            return tokens

        finally:
            driver.quit()
            
    except Exception as e:
        logger.error(f"Browser automation failed: {e}")
        raise e
