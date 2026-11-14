import os
import re
import sys
import json
import time
import base64
import hashlib
import requests
import click
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\/:*?"<>| ]', '_', name)

# ---------------------------------------------------------
# Mobiotics Handler (Ultra Play / Ultra Jhakaas)
# ---------------------------------------------------------
def get_mobiotics_token(provider_id: str, aes_key: str) -> str:
    import random
    device_id = "".join(random.choices("0123456789", k=16))
    payload = {
        "deviceid": device_id,
        "devicetype": "PC",
        "deviceos": "WINDOWS",
        "providerid": provider_id,
        "timestamp": int(time.time()),
        "appversion": "46.4.0"
    }
    json_str = json.dumps(payload, separators=(',', ':'))
    sha1_hash = hashlib.sha1(json_str.encode('utf-8')).hexdigest()
    
    key_bytes = aes_key.encode('utf-8')
    cipher = AES.new(key_bytes, AES.MODE_ECB)
    padded_data = pad(json_str.encode('utf-8'), AES.block_size)
    encrypted_bytes = cipher.encrypt(padded_data)
    
    b64_str = base64.b64encode(encrypted_bytes).decode('utf-8')
    enc_str = b64_str.replace('+', '-').replace('/', '_').replace('=', ',')
    
    device_url = f"https://vsms.mobiotics.com/prodv3/subscriberv2/v1/device/register/{provider_id}"
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "origin": "https://www.ultraplay.in" if provider_id == "ultrahin" else "https://www.ultrajhakaas.com",
        "referer": "https://www.ultraplay.in/" if provider_id == "ultrahin" else "https://www.ultrajhakaas.com/"
    }
    r = requests.post(device_url, params={"hash": sha1_hash}, data=enc_str, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()["success"]

def fetch_mobiotics_poster(url: str, provider_id: str, aes_key: str):
    click.echo(f"Initializing Mobiotics client for provider: {provider_id}...")
    token = get_mobiotics_token(provider_id, aes_key)
    
    # Parse slug
    slug = url.rstrip("/").split("/")[-1]
    query = slug.replace("-", " ")
    click.echo(f"Searching for '{query}'...")
    
    search_url = "https://vcms.mobiotics.com/prodv3/subscriber/v1/content/search"
    headers = {
        "user-agent": "Mozilla/5.0",
        "authorization": f"Bearer {token}",
        "x-provider-id": provider_id
    }
    r = requests.get(search_url, headers=headers, params={"query": query, "page": 1, "size": 20}, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = data.get("data") or []
    
    # Look for matching item
    target_item = None
    for item in items:
        item_title = item.get("title") or ""
        item_default = item.get("defaulttitle") or ""
        if (item_title.lower().replace(" ", "-") == slug.lower() or 
                item_default.lower().replace(" ", "-") == slug.lower() or
                item.get("objectid") == slug):
            target_item = item
            break
            
    if not target_item and items:
        target_item = items[0]
        click.echo("Exact match not found in search. Using first result.")
        
    if not target_item:
        raise ValueError(f"Content not found for slug: {slug}")
        
    title = target_item.get("title") or target_item.get("defaulttitle") or slug
    
    # Find highest resolution HD poster
    img_url = None
    posters = target_item.get("poster") or []
    # Prefer Portrait HD
    for p in posters:
        if p.get("postertype") == "PORTRAIT":
            for f in p.get("filelist") or []:
                if f.get("quality") == "HD":
                    img_url = f.get("filename")
                    break
            if img_url:
                break
                
    # Fallback to Landscape HD
    if not img_url:
        for p in posters:
            if p.get("postertype") == "LANDSCAPE":
                for f in p.get("filelist") or []:
                    if f.get("quality") == "HD":
                        img_url = f.get("filename")
                        break
                if img_url:
                    break
                    
    # Fallback to thumbnail
    if not img_url:
        img_url = target_item.get("thumbnail")
        
    if not img_url:
        raise ValueError("No poster or thumbnail URL found in metadata")
        
    return title, img_url

# ---------------------------------------------------------
# Generic Web Scraper (BeautifulSoup og:image tags)
# ---------------------------------------------------------
def fetch_generic_poster(url: str):
    click.echo("Fetching page HTML...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Try finding title
    title = None
    title_tag = soup.find('title')
    if title_tag:
        title = title_tag.text.strip()
        # Clean common trailer suffixes
        title = re.split(r' - | \| | Watch', title)[0]
        
    if not title:
        title = url.rstrip("/").split("/")[-1]
        
    # Search for og:image
    img_url = None
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        img_url = og_image['content']
    else:
        # Check standard meta property/name variations
        og_image = soup.find('meta', attrs={'name': 'og:image'})
        if og_image and og_image.get('content'):
            img_url = og_image['content']
            
    # Try json-ld script block fallback
    if not img_url:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                ld_data = json.loads(script.string or '')
                if isinstance(ld_data, dict):
                    if 'image' in ld_data:
                        img_url = ld_data['image']
                        break
                    elif '@graph' in ld_data:
                        for graph_item in ld_data['@graph']:
                            if 'image' in graph_item:
                                img_url = graph_item['image']
                                break
            except Exception:
                pass
                
    if not img_url:
        raise ValueError("Could not find og:image or JSON-LD image on this page")
        
    return title, img_url

# ---------------------------------------------------------
# KableOne Handler
# ---------------------------------------------------------
def fetch_kableone_poster(url: str):
    click.echo("Parsing KableOne URL...")
    
    # Check if webseries
    webseries_match = re.search(r"/Webseries/Webseriescontent/(\d+)", url)
    if not webseries_match:
        webseries_match = re.search(r"series:(\d+)", url)
        
    if webseries_match:
        series_id = webseries_match.group(1)
        click.echo(f"Querying KableOne webseries API for series ID: {series_id}...")
        api_url = "https://www.kableone.com/Webseries/GetWebseriesDetailsV4"
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Origin": "https://www.kableone.com"
        }
        r = requests.get(api_url, params={"webSeriesID": series_id}, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        detail = data.get("webseriesDetails", {}).get("detail") or {}
        if not detail:
            raise ValueError(f"Empty detail returned from KableOne for webseries ID {series_id}")
            
        title = detail.get("webseriesName") or f"KableOne_Series_{series_id}"
        img_url = detail.get("fullPageBanner") or detail.get("webThumbnail") or detail.get("mobileThumbnail")
        if not img_url:
            raise ValueError(f"No image URLs found in KableOne details for webseries ID {series_id}")
        return title, img_url

    # Otherwise movie or episode
    media_id = None
    m = re.search(r"/Watch/Content/(\d+)", url)
    if m:
        media_id = m.group(1)
    if not media_id:
        m = re.search(r"/Movies/Moviecontent/(\d+)", url)
        if m:
            media_id = m.group(1)
    if not media_id:
        m = re.search(r"id=(\d+)", url)
        if m:
            media_id = m.group(1)
            
    if not media_id:
        m = re.search(r"/(\d+)", url)
        if m:
            media_id = m.group(1)
            
    if not media_id:
        raise ValueError(f"Could not extract media ID from KableOne URL: {url}")
        
    click.echo(f"Querying KableOne media API for media ID: {media_id}...")
    api_url = "https://www.kableone.com/Movies/LoadRelatedItems"
    params = {"mediaId": media_id}
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Referer": url,
        "Origin": "https://www.kableone.com"
    }
    
    r = requests.get(api_url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    
    detail = data.get("mediaDetails", {}).get("detail") or {}
    if not detail:
        raise ValueError(f"Empty detail returned from KableOne for media ID {media_id}")
        
    title = detail.get("title") or detail.get("webSeriesName") or f"KableOne_{media_id}"
    img_url = detail.get("fullPageBanner") or detail.get("webThumbnail") or detail.get("mobileThumbnail")
    if not img_url:
        raise ValueError(f"No image URLs found in KableOne details for media ID {media_id}")
        
    return title, img_url

# ---------------------------------------------------------
# CLI Main Command
# ---------------------------------------------------------
@click.command()
@click.argument("url")
@click.option(
    "-o", "--output-dir",
    type=click.Path(writable=True, file_okay=False),
    default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads", "posters"),
    help="Target directory to save the poster image."
)
def main(url, output_dir):
    """
    Fetch the highest resolution poster image from any OTT URL.
    """
    click.echo(f"Target URL: {url}")
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    title = None
    img_url = None
    platform = "Generic"
    
    try:
        # Detect platform
        if "ultraplay.in" in domain:
            platform = "UltraPlay"
            title, img_url = fetch_mobiotics_poster(url, "ultrahin", "eWJtYsmvjzPZvcOW")
        elif "ultrajhakaas.com" in domain:
            platform = "UltraJhakaas"
            title, img_url = fetch_mobiotics_poster(url, "ultrajhs", "VrT02swZ6SUXYQBL")
        elif "kableone.com" in domain:
            platform = "KableOne"
            title, img_url = fetch_kableone_poster(url)
        elif "netflix.com" in domain:
            platform = "Netflix"
            title, img_url = fetch_generic_poster(url)
        elif "zee5.com" in domain:
            platform = "Zee5"
            title, img_url = fetch_generic_poster(url)
        elif "amazon" in domain or "primevideo" in domain:
            platform = "PrimeVideo"
            title, img_url = fetch_generic_poster(url)
        elif "apple.com" in domain:
            platform = "AppleTV"
            title, img_url = fetch_generic_poster(url)
        elif "hotstar.com" in domain:
            platform = "Hotstar"
            title, img_url = fetch_generic_poster(url)
        else:
            click.echo("Platform not matched specifically. Falling back to generic og:image scraper.")
            title, img_url = fetch_generic_poster(url)
            
        click.echo(f"Detected Platform: {platform}")
        click.echo(f"Parsed Title: {title}")
        click.echo(f"Poster URL: {img_url}")
        
        # Download and save image
        click.echo("Downloading image...")
        r = requests.get(img_url, stream=True, timeout=20)
        r.raise_for_status()
        
        # Determine file extension from content-type or URL
        ext = "jpg"
        content_type = r.headers.get("content-type", "")
        if "png" in content_type:
            ext = "png"
        elif "webp" in content_type:
            ext = "webp"
        else:
            # Fallback to suffix of URL path
            parsed_img = urlparse(img_url)
            path_ext = os.path.splitext(parsed_img.path)[1]
            if path_ext:
                ext = path_ext.lstrip(".")
                
        safe_title = sanitize_filename(title)
        filename = f"{platform}_{safe_title}.{ext}"
        
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)
        
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    
        click.echo(click.style(f"Success! Saved poster to: {filepath}", fg="green"))
        
    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
