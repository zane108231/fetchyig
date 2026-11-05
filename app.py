import os
from curl_cffi import requests
from flask import Flask, render_template, request, flash, jsonify
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import logging
import base64
import re
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["20 per minute"])

def get_instagram_headers():
    return {
        "user-agent": os.getenv("INSTAGRAM_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "cookie": os.getenv("INSTAGRAM_COOKIE", ""),
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.instagram.com/",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1"
    }

def get_media_base64(url, media_type="image"):
    try:
        if not url:
            return None
            
        response = requests.get(url, impersonate="chrome", stream=True, headers=get_instagram_headers(), timeout=10)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', f'{media_type}/jpeg')
        base64_encoded = base64.b64encode(response.content).decode('utf-8')
        
        return f"data:{content_type};base64,{base64_encoded}"
    except Exception as e:
        logger.error(f"Error processing {media_type}: {str(e)}")
        return None

def fetch_instagram_profile(username):
    username = username.strip().lstrip('@')
    profile_web_url = f"https://www.instagram.com/{username}/"
    
    res = requests.get(profile_web_url, impersonate="chrome", headers=get_instagram_headers(), timeout=10)
    if res.status_code == 404:
        return None, "Profile not found"
    
    res.raise_for_status()
    soup = BeautifulSoup(res.text, 'html.parser')
    
    meta_desc = soup.find('meta', property='og:description')
    meta_image = soup.find('meta', property='og:image')
    meta_title = soup.find('meta', property='og:title')
    
    if not meta_title or not meta_desc:
        return None, "Could not parse Instagram meta layout"
        
    title_text = meta_title.get('content', '')
    desc_text = meta_desc.get('content', '')
    
    full_name = title_text.split('•')[0].strip()
    name_clean = full_name.split('(')[0].strip() if '(' in full_name else full_name

    followers_match = re.search(r'([\d,KkMm\.]+)\s+Followers', desc_text, re.IGNORECASE)
    following_match = re.search(r'([\d,KkMm\.]+)\s+Following', desc_text, re.IGNORECASE)
    posts_match = re.search(r'([\d,KkMm\.]+)\s+Posts', desc_text, re.IGNORECASE)
    
    followers = followers_match.group(1) if followers_match else "0"
    following = following_match.group(1) if following_match else "0"
    posts = posts_match.group(1) if posts_match else "0"
    
    pic_url = meta_image.get('content') if meta_image else ""
    base64_pic = get_media_base64(pic_url) if pic_url else None
    
    profile_data = {
        "username": username,
        "name": name_clean,
        "followers": followers,
        "following": following,
        "posts": posts,
        "profile_pic_url": pic_url,
        "profile_pic_base64": base64_pic or pic_url
    }
    return profile_data, None

# --- Keep-Alive Health Endpoint for UptimeRobot ---
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "active"}), 200

@app.route("/api/<username>", methods=["GET"])
@limiter.limit("20 per minute")
def api_profile(username):
    profile, error = fetch_instagram_profile(username)
    if error:
        return jsonify({"error": error}), 404
    return jsonify(profile)

@app.route("/", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def index():
    profile_data = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lstrip('@')
        if not username:
            flash("Please enter a valid username", "error")
            return render_template("index.html")
            
        profile_data, error = fetch_instagram_profile(username)
        if error:
            flash(error, "error")
            return render_template("index.html")

    return render_template("index.html", profile=profile_data)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
