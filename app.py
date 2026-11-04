import os
import re
import requests
import logging
import hmac
from datetime import datetime
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()
api_key = os.getenv("IG_API_KEY", "")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

# Cache for uptime checks
uptime_cache = {
    'last_check': None,
    'status': 'online',
    'requests_served': 0
}

# Rate limiting configuration
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["100 per day", "20 per hour"],
    storage_uri="memory://",
    strategy="fixed-window"
)

def limit_exempt_uptime():
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if request.path == '/uptime':
                return f(*args, **kwargs)
            return limiter.limit("10 per minute")(f)(*args, **kwargs)
        return wrapped
    return decorator

@app.errorhandler(429)
def handle_rate_limit(error):
    if request.path.startswith("/api/"):
        return jsonify({
            "success": False,
            "error": "Rate limit exceeded",
            "retry_after": error.description
        }), 429
    return error

@app.route('/')
def home():
    uptime_cache['last_check'] = datetime.now().isoformat()
    uptime_cache['requests_served'] = uptime_cache.get('requests_served', 0) + 1
    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Instagram Scraper API Status</title></head>
    <body style="font-family: Arial; text-align: center; padding: 50px;">
        <h2>Instagram Public Scraper API is ONLINE</h2>
        <p>Requests served: {uptime_cache['requests_served']}</p>
        <p>Server time: {datetime.now().isoformat()}</p>
    </body>
    </html>
    """

@app.route('/api/health')
def api_health():
    return jsonify({"success": True, "status": "online"}), 200

@app.route('/api/instagram/<username>')
@limit_exempt_uptime()
def api_instagram_profile(username):
    provided_key = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    is_forwarded_request = bool(
        request.headers.get("X-Forwarded-For")
        or request.headers.get("X-Forwarded-Host")
    )

    # Keep direct localhost use convenient, but require a key for tunnel/proxy traffic.
    if is_forwarded_request and not api_key:
        return jsonify({
            "success": False,
            "error": "API is not configured. Set IG_API_KEY in the server environment."
        }), 503
    if is_forwarded_request and not hmac.compare_digest(provided_key, api_key):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
        return jsonify({"success": False, "error": "Invalid Instagram username format"}), 400
    
    debug_info = {
        "request_time": datetime.now().isoformat(),
        "errors": []
    }
    
    try:
        target_url = f"https://insta-stories-viewer.com/{username}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Referer": "https://insta-stories-viewer.com/"
        }
        
        response = requests.get(target_url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            status_code = 503 if response.status_code == 429 else 502
            return jsonify({
                "success": False,
                "error": f"Viewer site returned status {response.status_code}",
                "debug": debug_info
            }), status_code
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # The viewer embeds hidden "account not found" placeholders on valid pages.
        # Only treat that message as an error when the profile itself is absent.
        nickname_elem = soup.find(class_="profile__nickname")
        profile_tabs = soup.select_one(".profile__tabs[data-username]")
        if not nickname_elem and not profile_tabs:
            page_text = soup.get_text(" ", strip=True).lower()
            if "an account with this login does not exist" in page_text:
                return jsonify({
                    "success": False,
                    "error": "Instagram user not found on viewer site",
                    "debug": debug_info
                }), 404
            debug_info["errors"].append("Viewer page did not contain recognizable profile markup.")
            return jsonify({
                "success": False,
                "error": "Could not find profile details on viewer site",
                "debug": debug_info
            }), 502

        page_username = profile_tabs.get("data-username") if profile_tabs else None
        if not page_username and nickname_elem:
            page_username = nickname_elem.get_text(" ", strip=True).split()[0]
        page_username = page_username or username

        # The source page does not expose a separate full name, so use its nickname.
        full_name = page_username

        posts_elem = soup.find(class_="profile__stats-posts")
        posts_count = posts_elem.get_text(strip=True) if posts_elem else "N/A"
        followers_elem = soup.find(class_="profile__stats-followers")
        followers_count = followers_elem.get_text(strip=True) if followers_elem else "N/A"

        following_elem = soup.find(class_="profile__stats-follows")
        following_count = following_elem.get_text(strip=True) if following_elem else "N/A"

        avatar_img = soup.find(class_="profile__avatar-pic")
        profile_pic_url = ""
        if avatar_img:
            profile_pic_url = avatar_img.get("src") or avatar_img.get("data-src") or avatar_img.get("data-lazy-src") or ""

        desc_elem = soup.find(class_="profile__description")
        bio_text = desc_elem.get_text(strip=True) if desc_elem else ""

        # If fallback fields are empty, check if the page blocked scraper requests
        if followers_count == "N/A" and not profile_pic_url:
            debug_info["errors"].append("Parsed empty elements, layout might have changed or bot protection triggered.")
            return jsonify({
                "success": False,
                "error": "Failed to parse profile elements from viewer structure",
                "debug": debug_info
            }), 502

        profile_data = {
            "username": page_username,
            "full_name": full_name,
            "profile_pic": profile_pic_url,
            "bio": bio_text,
            "posts": posts_count,
            "posts_count": posts_count,
            "followers": followers_count,
            "following": following_count
        }
        
        return jsonify({
            "success": True,
            "profile": profile_data
        })
        
    except Exception as e:
        logger.error(f"Fatal error parsing profile: {str(e)}")
        debug_info["errors"].append(str(e))
        return jsonify({"success": False, "error": str(e), "debug": debug_info}), 502

@app.route('/uptime')
def uptime_check():
    uptime_cache['last_check'] = datetime.now().isoformat()
    uptime_cache['requests_served'] += 1
    return jsonify(uptime_cache), 200

if __name__ == "__main__":
    app.run(host='127.0.0.1', port=int(os.getenv("PORT", "5000")), debug=False)
