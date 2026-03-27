# Cell 1: Setup and Configuration
import requests
import json
import base64
import re
from datetime import datetime
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration from environment variables
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
SHEET_ID = os.getenv("SHEET_ID", "")

# Cell 2: Define Functions for Modules

def download_whatsapp_image(image_id):
    """Replicates Module 38 and 40: Get image URL and download"""
    # Step 1: Get download URL
    url_info = f"https://graph.facebook.com/v22.0/{image_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    response = requests.get(url_info, headers=headers)
    
    if response.status_code == 200:
        download_url = response.json().get('url')
        # Step 2: Download the file
        img_response = requests.get(download_url, headers=headers)
        return img_response.content
    return None

def identify_product_with_groq(image_bytes):
    """Replicates Module 41 and 43: Base64 conversion and Groq API call"""
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Identify the product in this image. Focus on colour, brand, and type."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        }]
    }
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    response = requests.post(GROQ_URL, headers=headers, json=payload)
    return response.json()

def extract_urls(text):
    """Replicates Module 126: Regex link matching"""
    pattern = r"(https?://\S+)"
    return re.findall(pattern, text)

# Cell 3: Main Execution (Simulating the Webhook Trigger)

# Sample incoming data structure based on the 'json:ParseJSON' module
incoming_data = {
    "entry": [{
        "changes": [{
            "value": {
                "messages": [{
                    "type": "image",
                    "image": {"id": "SAMPLE_IMAGE_ID", "caption": "Check this out"},
                    "from": "123456789"
                }]
            }
        }]
    }]
}

# Logic Flow based on Basic Router (Module 36)
msg = incoming_data['entry'][0]['changes'][0]['value']['messages'][0]

if msg['type'] == 'image':
    print("Processing Image Branch...")
    img_data = download_whatsapp_image(msg['image']['id'])
    if img_data:
        result = identify_product_with_groq(img_data)
        print("Groq Identification:", result['choices'][0]['message']['content'])

elif msg['type'] == 'text':
    print("Processing Text Branch...")
    body = msg['text']['body']
    links = extract_urls(body)
    print(f"Logged Text: {body}")
    print(f"Extracted Links: {links}")
