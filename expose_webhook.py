import os
import time
import requests
from dotenv import load_dotenv
from pyngrok import ngrok

load_dotenv()

def write_env(key: str, value: str, env_file: str = ".env"):
    lines = []
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(env_file, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

def start_https_tunnel(port: int):
    # Newer pyngrok supports bind_tls kwarg; older versions may need options={"bind_tls": True}
    try:
        tunnel = ngrok.connect(port, bind_tls=True)
    except TypeError:
        tunnel = ngrok.connect(port, options={"bind_tls": True})
    public_url = tunnel.public_url
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[len("http://"):]
    return tunnel, public_url

def set_telegram_webhook(bot_token: str, webhook_url: str, secret_token: str | None = None):
    api = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    data = {"url": webhook_url}
    if secret_token:
        data["secret_token"] = secret_token
    r = requests.post(api, data=data, timeout=30)
    r.raise_for_status()
    return r.json()

def main(port: int = 8000):
    ngrok_token = os.getenv("NGROK_TOKEN", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_API_KEY", "").strip()
    webhook_path = os.getenv("WEBHOOK_PATH", "/telegram").strip()
    secret_token = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip() or None
    user_id = os.getenv("USER_ID", "1").strip()

    if not bot_token:
        raise SystemExit("Missing TELEGRAM_BOT_API_KEY in .env")

    if ngrok_token:
        ngrok.set_auth_token(ngrok_token)

    tunnel = None
    try:
        tunnel, public_url = start_https_tunnel(port)
        write_env("PUBLIC_URL", public_url)

        if not webhook_path.startswith("/"):
            webhook_path = "/" + webhook_path
        
        # Correctly route to /api/webhooks/<user_id>/<path>
        webhook_url = f"{public_url}/api/webhooks/{user_id}{webhook_path}"
        write_env("TELEGRAM_WEBHOOK_URL", webhook_url)

        print(f"[+] Public URL: {public_url}")
        print(f"[+] Webhook URL: {webhook_url}")

        resp = set_telegram_webhook(bot_token, webhook_url, secret_token=secret_token)
        print(f"[+] setWebhook response: {resp}")

        print("[!] Keep this running. Ctrl+C to stop.")
        while True:
            time.sleep(5)

    except KeyboardInterrupt:
        print("\n[!] Stopping...")
    finally:
        if tunnel:
            ngrok.disconnect(tunnel.public_url)
        ngrok.kill()

if __name__ == "__main__":
    main(8000)
