# WhatsApp Sales Agent

A Python-based sales agent that integrates with WhatsApp to process customer images and identify products using AI-powered image recognition.

## Features

- **Image Processing**: Download and process images from WhatsApp messages
- **AI Product Identification**: Uses Groq's Llama model to identify products from images
- **URL Extraction**: Extract links from text messages
- **Environment Configuration**: Secure handling of API credentials

## Prerequisites

- Python 3.8 or higher
- WhatsApp Business API token
- Groq API key
- Google Sheets ID (for data logging)

## Setup Instructions

### 1. Create Virtual Environment

```bash
# Create a virtual environment
python3 -m venv venv

# Activate the virtual environment
# On macOS/Linux:
source venv/bin/activate

# On Windows:
venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file in the project root directory with your API credentials:

```bash
WHATSAPP_TOKEN=your_whatsapp_token_here
GROQ_API_KEY=your_groq_api_key_here
SHEET_ID=your_sheet_id_here
GROQ_URL=https://api.groq.com/openai/v1/chat/completions
```

**Note**: The `.env` file is ignored by Git and should never be committed to version control.

### 4. Run the Agent

```bash
python sales_agent.py
```

## Project Structure

```
whatsapp_sales_agent/
├── sales_agent.py          # Main agent script
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (not in Git)
├── .gitignore             # Git ignore rules
└── README.md              # This file
```

## Dependencies

- **requests**: HTTP library for API calls
- **python-dotenv**: Environment variable management

## How It Works

1. **Webhook Trigger**: Receives incoming WhatsApp messages
2. **Message Router**: Routes messages based on type (image or text)
3. **Image Processing**: 
   - Downloads image from WhatsApp
   - Converts to base64
   - Sends to Groq API for product identification
4. **Text Processing**: Extracts URLs from text messages

## License

See LICENSE file for details.